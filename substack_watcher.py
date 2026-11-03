#!/usr/bin/env python3
"""Post Substack activity to a Discord channel as preview cards.

Who to follow is set in substack_feeds.json. Add a line there to follow someone
new. A person's current items are saved without posting the first time they're
checked, so adding someone doesn't flood the channel.

Notes come from Open RSS, a free outside service, so each feed is checked at most
once an hour and only downloaded if it has changed.

  python3 substack_watcher.py --dry-run   Print the latest items. Posts nothing.
  python3 substack_watcher.py --test      Post the latest item from each feed.
  python3 substack_watcher.py             Normal run.

The webhook URL is read from the DISCORD_SUBSTACK_WEBHOOK_URL environment variable.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from watcher import post_to_discord

FEEDS_FILE = Path(__file__).with_name("substack_feeds.json")
STATE_FILE = Path(__file__).with_name("substack_state.json")

NOTES_URL = "https://openrss.org/substack.com/@{handle}"
POSTS_URL = "https://openrss.org/{publication}.substack.com"
USER_AGENT = ("TradingViewDiscordAlerts/1.0 "
              "(+https://github.com/mark-musumeci/tradingview-discord-alerts)")

CHECK_EVERY_SECONDS = 60 * 60
FEED_GAP_SECONDS = 2     # Open RSS asks for a gap between requests.
MAX_SEEN = 300
MAX_POSTS_PER_FEED = 5
MAX_FEED_BYTES = 5_000_000
TIMEOUT = 30
ORANGE = 0xFF6719

MEDIA = "{http://search.yahoo.com/mrss/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
ATOM = "{http://www.w3.org/2005/Atom}"


class RateLimited(RuntimeError):
    """Open RSS asked us to stop, so no more requests are made in this run."""


def load_feeds():
    """Return one entry per feed from the people listed in substack_feeds.json."""
    try:
        people = json.loads(FEEDS_FILE.read_text())
    except ValueError as error:
        raise RuntimeError(f"substack_feeds.json isn't valid: {error}") from error

    feeds = []
    for person in people:
        if person.get("handle"):
            feeds.append({**person, "kind": "note",
                          "url": NOTES_URL.format(handle=person["handle"])})
        if person.get("publication"):
            feeds.append({**person, "kind": "post",
                          "url": POSTS_URL.format(publication=person["publication"])})
    return feeds


def download(feed, saved):
    """Return (feed text or None if unchanged, new saved values)."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/rss+xml, application/atom+xml, application/xml"}
    if saved.get("etag"):
        headers["If-None-Match"] = saved["etag"]
    if saved.get("modified"):
        headers["If-Modified-Since"] = saved["modified"]
    request = urllib.request.Request(feed["url"], headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            text = response.read(MAX_FEED_BYTES).decode("utf-8", "replace")
            match = re.search(r"max-age=(\d+)", response.headers.get("Cache-Control") or "")
            if match:
                saved["next_check"] = max(saved.get("next_check", 0),
                                          time.time() + int(match.group(1)))
            return text, {"etag": response.headers.get("ETag"),
                          "modified": response.headers.get("Last-Modified")}
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, {}
        if error.code in (429, 503):
            wait = error.headers.get("Retry-After") or ""
            delay = int(wait) if wait.isdigit() else 0
            saved["next_check"] = time.time() + max(delay, CHECK_EVERY_SECONDS)
            raise RateLimited(f"HTTP {error.code}") from error
        raise RuntimeError(f"HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(str(error)) from error


def find_text(node, *names):
    for name in names:
        found = node.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def plain_text(markup):
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", markup or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def find_image(node, markup):
    """Return a preview image from the feed item, if it has one."""
    for tag in (f"{MEDIA}content", f"{MEDIA}thumbnail", "enclosure"):
        found = node.find(tag)
        if found is None:
            continue
        url = found.get("url", "")
        kind = found.get("type", "image")
        if url.startswith("https://") and kind.startswith("image"):
            return url
    match = re.search(r"""<img[^>]+src=["'](https://[^"']+)""", markup or "")
    return html.unescape(match.group(1)) if match else None


def item_date(node):
    """Return an ISO date for Discord's timestamp, or None if it can't be read."""
    raw = find_text(node, "pubDate", f"{ATOM}published", f"{ATOM}updated")
    if not raw:
        return None
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return raw if re.match(r"\d{4}-\d{2}-\d{2}T", raw) else None
    return moment.astimezone(timezone.utc).isoformat()


def item_link(node):
    link = find_text(node, "link")
    if link:
        return link
    atom = node.find(f"{ATOM}link")
    return atom.get("href", "") if atom is not None else ""


def parse_feed(text):
    """Return (feed image, items newest first)."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise RuntimeError(f"couldn't read the feed: {error}") from error

    logo = root.find("./channel/image/url")
    items = []
    for node in list(root.iter("item")) + list(root.iter(f"{ATOM}entry")):
        markup = (find_text(node, f"{CONTENT}encoded", f"{ATOM}content")
                  or find_text(node, "description", f"{ATOM}summary"))
        body = plain_text(markup)
        link = item_link(node)
        key = find_text(node, "guid", f"{ATOM}id") or link
        if not key or not link.startswith("https://"):
            continue
        items.append({
            "id": key, "link": link, "title": plain_text(find_text(node, "title", f"{ATOM}title")),
            "body": body, "image": find_image(node, markup), "date": item_date(node),
        })
    return (logo.text.strip() if logo is not None and logo.text else None), items


def is_wanted(item, feed):
    """Leave out restacks and comments unless the person's entry asks for them."""
    opening = f"{item['title']} {item['body']}".lstrip().lower()
    if opening.startswith("restacked"):
        return bool(feed.get("include_restacks"))
    if opening.startswith("commented on"):
        return bool(feed.get("include_comments"))
    return True


def clip(text, limit):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def build_card(item, feed, logo):
    """Build a Discord card: who posted, the title, a preview image and when."""
    title = clip(item["title"] or item["body"].split("\n")[0] or "New note", 240)
    label = "published a new post" if feed["kind"] == "post" else "posted a new note"
    card = {
        "color": ORANGE,
        "author": {"name": f"{feed['name']} {label} on Substack"[:256]},
        "title": title,
        "url": item["link"],
        "footer": {"text": "Published"},
    }
    if logo:
        card["author"]["icon_url"] = logo
    if item["body"] and " ".join(item["body"].split()) != title:
        card["description"] = clip(item["body"], 500)
    if item["image"]:
        card["image"] = {"url": item["image"]}
    if item["date"]:
        card["timestamp"] = item["date"]
    return card


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=0) + "\n")


def check_feed(webhook, feed, state, now):
    saved = state.get(feed["url"])
    saved = saved if saved is not None else {"seen": [], "started": False}
    if now < saved.get("next_check", 0):
        return

    # Saved even if the check fails, so a failed feed still waits before retrying.
    saved["next_check"] = now + CHECK_EVERY_SECONDS
    state[feed["url"]] = saved
    text, cache = download(feed, saved)
    if text is None:
        return
    saved.update(cache)
    logo, items = parse_feed(text)
    print(f"Read {feed['url']}: {len(items)} items.")

    if not saved.get("started", True):
        saved["seen"] = [i["id"] for i in items][:MAX_SEEN]
        saved["started"] = bool(items)
        print(f"First check of {feed['url']}: saved {len(items)} items without posting.")
        return

    new = [i for i in items if i["id"] not in saved["seen"]]
    new.reverse()  # Oldest first, so the channel reads in order.
    skipped, to_post = new[:-MAX_POSTS_PER_FEED], new[-MAX_POSTS_PER_FEED:]
    saved["seen"] += [i["id"] for i in skipped]
    for item in to_post:
        if is_wanted(item, feed):
            post_to_discord(webhook, "", [build_card(item, feed, logo)])
            print(f"Posted: {feed['name']}: {clip(item['title'] or item['body'], 70)}")
            time.sleep(1)
        saved["seen"].append(item["id"])
    saved["seen"] = saved["seen"][-MAX_SEEN:]


def run(webhook):
    state = load_state()
    now = time.time()
    try:
        for index, feed in enumerate(load_feeds()):
            if index:
                time.sleep(FEED_GAP_SECONDS)
            try:
                check_feed(webhook, feed, state, now)
            except RateLimited as error:
                print(f"Open RSS asked us to wait ({error}), so this run stops here.")
                break
            except RuntimeError as error:
                print(f"Skipped {feed['url']} this time: {error}")
    finally:
        save_state(state)


def latest_item(feed):
    """Return the newest wanted item and the feed image, without using saved values."""
    text, _ = download(feed, {})
    logo, items = parse_feed(text)
    wanted = [i for i in items if is_wanted(i, feed)]
    return (wanted[0] if wanted else None), logo


def preview(webhook):
    """Print or post the latest item from each feed."""
    state = load_state()
    for feed in load_feeds():
        time.sleep(FEED_GAP_SECONDS)
        try:
            item, logo = latest_item(feed)
        except RuntimeError as error:
            print(f"Couldn't read {feed['url']}: {error}")
            continue
        if item is None:
            print(f"{feed['url']}: nothing to show.")
            continue
        card = build_card(item, feed, logo)
        print(json.dumps(card, indent=1, ensure_ascii=False))
        if webhook:
            post_to_discord(webhook, "", [card])
            # A test post counts as sent, so a normal run won't post it again.
            saved = state.get(feed["url"])
            if saved is not None and item["id"] not in saved["seen"]:
                saved["seen"].append(item["id"])
            time.sleep(1)
    if webhook:
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Post Substack activity to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="print the latest items only")
    parser.add_argument("--test", action="store_true", help="post the latest item from each feed")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_SUBSTACK_WEBHOOK_URL", "").strip()
    try:
        if args.dry_run:
            preview("")
        elif not webhook:
            print("No Substack webhook set, so skipping.")
        elif args.test:
            preview(webhook)
        else:
            run(webhook)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

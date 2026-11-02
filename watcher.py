#!/usr/bin/env python3
"""Post new TradingView US-stock top stories to a Discord channel.

Each run checks the feed once, posts any stories it has not seen, and records
them in seen.json. Run it on a schedule. The very first run only records what
is already in the feed, so the channel isn't flooded with old stories.

If GEMINI_API_KEY is set, stories get a verified AI tag (see ai.py), and those
tagged low impact are left out. Anything that can't be verified is posted as a
plain headline, so an AI failure never hides a story.

  python3 watcher.py --dry-run   Print the latest stories. Posts nothing.
  python3 watcher.py --test      Post the latest story, to check the webhook.
  python3 watcher.py             Normal run.

The webhook URL is read from the DISCORD_WEBHOOK_URL environment variable.
Uses only the Python standard library.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ai
from tv_data import SITE_URL, fetch_json

NEWS_URL = "https://news-mediator.tradingview.com/public/news-flow/v2/news"
STORY_URL = "https://news-mediator.tradingview.com/public/news/v1/story"
FEED_PAGE = f"{SITE_URL}/news-flow/?market=stock&market_country=us&priority=top_stories"

# The API rejects filters that aren't in alphabetical order.
FILTERS = sorted(["lang:en", "market:stock", "market_country:US", "priority:top_stories"])

STATE_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 1000      # IDs to remember. The feed returns up to 200 at a time.
MAX_POSTS = 10       # Most stories to post in one run, in case of a backlog.
TIMEOUT = 30
POST_ATTEMPTS = 5
MAX_SOURCE_CHARS = 6000

SENTIMENT = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", "neutral": "⚪ Neutral"}


def fetch_stories():
    """Return usable top stories from the feed, oldest first."""
    query = urllib.parse.urlencode(
        {"filter": FILTERS, "client": "screener", "streaming": "false",
         "user_prostatus": "non_pro"},
        doseq=True,
        safe=":",
    )
    data = fetch_json(f"{NEWS_URL}?{query}")

    stories = [i for i in data.get("items", []) if i.get("id") and i.get("title")]
    return sorted(stories, key=lambda i: i.get("published", 0))


def ast_text(node):
    """Turn TradingView's article structure into plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    params = node.get("params") or {}
    if node.get("type") == "symbol":
        return params.get("text") or params.get("symbol") or ""
    if node.get("type") == "url":
        return params.get("linkText") or params.get("url") or ""
    parts = [ast_text(child) for child in node.get("children", [])]
    joiner = "\n" if node.get("type") in ("root", "list") else ""
    return joiner.join(parts)


def story_text(story_id):
    """Return a story's text, or nothing if it can't be read."""
    query = urllib.parse.urlencode({"id": story_id, "lang": "en"})
    try:
        data = fetch_json(f"{STORY_URL}?{query}")
    except RuntimeError:
        return ""
    return ast_text(data.get("ast_description")) or data.get("short_description") or ""


def tag_story(story):
    """Return a verified AI tag for a story, or None to post it untagged."""
    if not ai.enabled():
        return None
    source = f"{story['title']}\n{story_text(story['id'])}"[:MAX_SOURCE_CHARS]
    tag = ai.check_tag(ai.ask_json(ai.TAG_PROMPT, source), source)
    time.sleep(ai.PACE_SECONDS)
    if tag is None:
        print(f"No verified tag: {story['title']}")
    return tag


def story_url(story):
    path = story.get("storyPath")
    if path:
        return path if path.startswith("http") else f"{SITE_URL}{path}"
    return story.get("link") or FEED_PAGE


def escape_markdown(text):
    return re.sub(r"([\\*_~`|])", r"\\\1", text)


def format_message(story, tag=None):
    """The headline and link, with the AI tag and its source text when verified."""
    title = escape_markdown(" ".join(story["title"].split()))[:1200]
    lines = []
    if tag:
        lines.append(f"{SENTIMENT[tag['sentiment']]} · {tag['impact'].title()} impact")
    lines.append(f"**{title}**")
    if tag:
        lines.append(f"Why it matters: {escape_markdown(tag['why'])}")
    lines.append(f"<{story_url(story)}>")

    provider = story.get("provider")
    details = [provider.get("name")] if isinstance(provider, dict) else []
    symbols = [s.get("symbol", "").split(":")[-1] for s in story.get("relatedSymbols") or []]
    if symbols:
        details.append(", ".join(s for s in symbols[:5] if s))
    details = [d for d in details if d]
    if details:
        lines.append("-# " + escape_markdown(" · ".join(details)))
    if tag:
        lines.append(f'-# Source text: "{escape_markdown(tag["quote"])}"')
    return "\n".join(lines)


def post_to_discord(webhook, content):
    """Send one message, waiting and retrying if Discord asks us to slow down."""
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        webhook,
        data=body,
        # Discord refuses Python's default User-Agent.
        headers={"Content-Type": "application/json", "User-Agent": "tv-top-stories/1.0"},
        method="POST",
    )

    for attempt in range(1, POST_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT):
                return
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            if not retryable or attempt == POST_ATTEMPTS:
                raise RuntimeError(f"Discord returned HTTP {error.code}") from error
            time.sleep(retry_delay(error, attempt))
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == POST_ATTEMPTS:
                raise RuntimeError(f"Could not reach Discord: {error}") from error
            time.sleep(2 ** attempt)


def retry_delay(error, attempt):
    try:
        return float(json.load(error)["retry_after"]) + 0.25
    except (ValueError, KeyError, TypeError):
        return float(error.headers.get("Retry-After") or 2 ** attempt)


def load_seen():
    """Return the list of seen IDs, or None if this is the first run."""
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text())


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen[-MAX_SEEN:], indent=0) + "\n")


def run(webhook):
    stories = fetch_stories()
    if not stories:
        print("The feed returned no stories, so nothing was changed.")
        return

    seen = load_seen()
    if seen is None:
        save_seen([s["id"] for s in stories])
        print(f"First run: recorded {len(stories)} existing stories without posting.")
        return

    known = set(seen)
    new = [s for s in stories if s["id"] not in known]
    skipped, to_post = new[:-MAX_POSTS], new[-MAX_POSTS:]
    seen += [s["id"] for s in skipped]
    if skipped:
        print(f"Backlog: skipped the {len(skipped)} oldest new stories.")

    try:
        for index, story in enumerate(to_post):
            if index:
                time.sleep(1)
            tag = tag_story(story)
            if tag and tag["impact"] == "low":
                seen.append(story["id"])
                print(f"Skipped as low impact: {story['title']}")
                continue
            post_to_discord(webhook, format_message(story, tag))
            seen.append(story["id"])
            print(f"Posted: {story['title']}")
    finally:
        # Keeps progress if a post fails, so nothing is posted twice.
        save_seen(seen)

    print(f"{len(to_post)} new, {len(stories)} in feed.")


def main():
    parser = argparse.ArgumentParser(description="Post TradingView top stories to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="print the latest stories only")
    parser.add_argument("--test", action="store_true", help="post the latest story to Discord")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        sys.exit("Set the DISCORD_WEBHOOK_URL environment variable first.")

    try:
        if args.dry_run:
            stories = fetch_stories()
            print(f"{len(stories)} top stories in the feed. Latest 5:\n")
            for story in stories[-5:]:
                print(format_message(story, tag_story(story)) + "\n")
        elif args.test:
            story = fetch_stories()[-1]
            post_to_discord(webhook, format_message(story, tag_story(story)))
            print("Test message sent.")
        else:
            run(webhook)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

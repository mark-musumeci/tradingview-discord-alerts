#!/usr/bin/env python3
"""Post Substack Notes to a Discord channel as preview cards.

Who to follow is set in substack_feeds.json. Add a line there to follow someone
new. Their existing notes are saved without posting the first time they're
checked, so adding someone doesn't flood the channel.

Notes come from an Apify Actor, checked about once an hour per person. Each run
logs its cost, so usage can be compared with Apify's monthly free credit.

  python3 substack_watcher.py --dry-run   Print each person's latest note.
  python3 substack_watcher.py --test      Post each person's latest note.
  python3 substack_watcher.py             Normal run.

Reads DISCORD_SUBSTACK_WEBHOOK_URL and APIFY_TOKEN from the environment.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watcher import post_to_discord

FEEDS_FILE = Path(__file__).with_name("substack_feeds.json")
STATE_FILE = Path(__file__).with_name("substack_state.json")

API_BASE = "https://api.apify.com/v2"
ACTOR = "automation-lab~substack-notes-search-author-monitor"
RUN_TIMEOUT_SECONDS = 150
POLL_SECONDS = 5
MAX_WAIT_SECONDS = 210

CHECK_EVERY_SECONDS = 58 * 60   # Runs start on a 5-minute timer, so this gives about hourly.
LOOKBACK_HOURS = 48
MAX_NOTES_PER_RUN = 10
MAX_POSTS_PER_PERSON = 5
MAX_SEEN = 300
TIMEOUT = 150
ORANGE = 0xFF6719


def load_people():
    try:
        return json.loads(FEEDS_FILE.read_text())
    except ValueError as error:
        raise RuntimeError(f"substack_feeds.json isn't valid: {error}") from error


def apify(path, token, payload=None):
    """Call Apify and return its JSON reply."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Apify returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"Could not reach Apify: {error}") from error


def fetch_notes(person, token, seen, limit=MAX_NOTES_PER_RUN, since=None):
    """Run the Actor for one person and return their notes, newest first."""
    payload = {"startUrls": [{"url": f"https://substack.com/@{person['handle']}"}],
               "maxItems": limit, "existingIds": seen[-MAX_SEEN:]}
    if since:
        payload["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    query = urllib.parse.urlencode({"waitForFinish": 120, "timeout": RUN_TIMEOUT_SECONDS})
    run = apify(f"/acts/{ACTOR}/runs?{query}", token, payload)["data"]

    waited = 0
    while run.get("status") in ("READY", "RUNNING") and waited < MAX_WAIT_SECONDS:
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        run = apify(f"/actor-runs/{run['id']}", token)["data"]
    run = apify(f"/actor-runs/{run['id']}", token)["data"]
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Apify run ended as {run.get('status')}")

    cost = run.get("usageTotalUsd")
    if cost is not None:
        print(f"Apify cost for {person['handle']}: ${cost:.4f} (about ${cost * 720:.2f} a month "
              "at hourly, against the $5 free credit)")

    items = apify(f"/datasets/{run['defaultDatasetId']}/items?clean=true", token)
    return own_notes(items, person)


def own_notes(items, person):
    """Keep this person's own notes, newest first."""
    owner = f"/@{person['handle'].lower()}/"
    notes = [i for i in items if i.get("id") and owner in str(i.get("url", "")).lower()]
    return sorted(notes, key=lambda n: n.get("publishedAt") or "", reverse=True)


def first_image(note):
    for url in note.get("mediaUrls") or []:
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def clip(text, limit):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def build_card(note, person):
    """Build a Discord card: who posted, the title, a preview image and when."""
    text = (note.get("text") or "").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    title = clip(lines[0], 240) if lines else "New note"
    card = {
        "color": ORANGE,
        "author": {"name": f"{person['name']} posted a new note on Substack"[:256]},
        "title": title,
        "url": note["url"],
        "footer": {"text": "Published"},
    }
    if " ".join(text.split()) != title and text:
        card["description"] = clip(text, 700)
    image = first_image(note)
    if image:
        card["image"] = {"url": image}
    if note.get("publishedAt"):
        card["timestamp"] = note["publishedAt"]
    return card


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=0) + "\n")


def carry_over(state, handle):
    """Move IDs saved under the old feed address to this person's record."""
    old = state.pop(f"https://openrss.org/substack.com/@{handle}", None)
    if old and handle not in state:
        ids = [re.sub(r".*c-", "", str(i)) for i in old.get("seen", [])]
        state[handle] = {"seen": ids, "started": True, "next_check": 0}


def check_person(webhook, token, person, state, now):
    handle = person["handle"]
    carry_over(state, handle)
    saved = state.setdefault(handle, {"seen": [], "started": False, "next_check": 0})
    if now < saved["next_check"]:
        return
    saved["next_check"] = now + CHECK_EVERY_SECONDS

    since = None
    if saved["started"]:
        since = datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    notes = fetch_notes(person, token, saved["seen"], since=since)
    new = [n for n in notes if str(n["id"]) not in saved["seen"]]
    print(f"Checked {handle}: {len(new)} new notes.")

    if not saved["started"]:
        saved["seen"] = [str(n["id"]) for n in notes]
        saved["started"] = bool(notes)
        print(f"First check of {handle}: saved {len(notes)} notes without posting.")
        return

    new.reverse()  # Oldest first, so the channel reads in order.
    skipped, to_post = new[:-MAX_POSTS_PER_PERSON], new[-MAX_POSTS_PER_PERSON:]
    saved["seen"] += [str(n["id"]) for n in skipped]
    for note in to_post:
        post_to_discord(webhook, "", [build_card(note, person)])
        saved["seen"].append(str(note["id"]))
        print(f"Posted: {person['name']}: {clip(note.get('text') or '', 70)}")
        time.sleep(1)
    saved["seen"] = saved["seen"][-MAX_SEEN:]


def run(webhook, token):
    state = load_state()
    now = time.time()
    try:
        for person in load_people():
            try:
                check_person(webhook, token, person, state, now)
            except RuntimeError as error:
                print(f"Skipped {person['handle']} this time: {error}")
    finally:
        save_state(state)


def preview(webhook, token):
    """Print or post each person's latest note."""
    state = load_state()
    for person in load_people():
        notes = fetch_notes(person, token, [], limit=1)
        if not notes:
            print(f"{person['handle']}: no notes found.")
            continue
        card = build_card(notes[0], person)
        print(json.dumps(card, indent=1, ensure_ascii=False))
        if webhook:
            post_to_discord(webhook, "", [card])
            # A test post counts as sent, so a normal run won't post it again.
            saved = state.get(person["handle"])
            if saved is not None and str(notes[0]["id"]) not in saved["seen"]:
                saved["seen"].append(str(notes[0]["id"]))
    if webhook:
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Post Substack Notes to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="print the latest notes only")
    parser.add_argument("--test", action="store_true", help="post each person's latest note")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_SUBSTACK_WEBHOOK_URL", "").strip()
    token = os.environ.get("APIFY_TOKEN", "").strip()
    try:
        if not token:
            print("No Apify token set, so skipping.")
        elif args.dry_run:
            preview("", token)
        elif not webhook:
            print("No Substack webhook set, so skipping.")
        elif args.test:
            preview(webhook, token)
        else:
            run(webhook, token)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

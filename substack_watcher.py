#!/usr/bin/env python3
"""Post Substack Notes to a Discord channel as preview cards.

Who to follow is set in substack_feeds.json. Add a line there to follow someone
new. Their existing notes are saved without posting the first time they're
checked, so adding someone doesn't flood the channel.

Notes come from an Apify Actor. Up to five people are checked in one shared run
about once an hour, so adding people doesn't add start fees. Spending is tracked
each month, and checks slow down if needed to stay under MONTHLY_BUDGET.

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

import status
from watcher import post_to_discord

FEEDS_FILE = Path(__file__).with_name("substack_feeds.json")
STATE_FILE = Path(__file__).with_name("substack_state.json")

API_BASE = "https://api.apify.com/v2"
ACTOR = "automation-lab~substack-notes-search-author-monitor"
RUN_TIMEOUT_SECONDS = 150
POLL_SECONDS = 5
MAX_WAIT_SECONDS = 210

CHECK_EVERY_SECONDS = 58 * 60   # Runs start on a 5-minute timer, so this gives about hourly.
MAX_PEOPLE = 5
START_FEE = 0.005
MONTHLY_BUDGET = 4.75           # Leaves a little of Apify's $5 credit for test runs.
LOOKBACK_HOURS = 48
NOTES_PER_PERSON = 10
MAX_POSTS_PER_PERSON = 5
MAX_SEEN = 300
TIMEOUT = 150
ORANGE = 0xFF6719


def load_people():
    try:
        people = json.loads(FEEDS_FILE.read_text())
    except ValueError as error:
        raise RuntimeError(f"substack_feeds.json isn't valid: {error}") from error
    if len(people) > MAX_PEOPLE:
        print(f"Only the first {MAX_PEOPLE} people are checked, to stay within the budget.")
    return people[:MAX_PEOPLE]


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


def fetch_notes(people, token, seen, since=None):
    """Run the Actor once for everyone. Return ({handle: notes}, cost of the run)."""
    payload = {
        "startUrls": [{"url": f"https://substack.com/@{p['handle']}"} for p in people],
        "maxItems": NOTES_PER_PERSON * len(people),
        "existingIds": seen,
    }
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

    # Apify can report 0 before its usage updates, so count at least the start fee.
    cost = max(run.get("usageTotalUsd") or 0.0, START_FEE)
    print(f"Apify run for {len(people)} people cost ${cost:.4f} "
          f"(events: {run.get('chargedEventCounts')})")
    items = apify(f"/datasets/{run['defaultDatasetId']}/items?clean=true", token)
    return {p["handle"]: own_notes(items, p) for p in people}, cost


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


def month_end(moment):
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (start + timedelta(days=32)).replace(day=1)


def next_gap(cost, spent, now):
    """Return the seconds to wait so the month's spending stays under the budget."""
    moment = datetime.fromtimestamp(now, timezone.utc)
    left = MONTHLY_BUDGET - spent
    if cost <= 0:
        return CHECK_EVERY_SECONDS
    if left < cost:
        return (month_end(moment) - moment).total_seconds()
    seconds_left = (month_end(moment) - moment).total_seconds()
    return max(CHECK_EVERY_SECONDS, seconds_left / (left / cost))


def record_cost(state, cost, now):
    """Add a run's cost to this month's total. Returns the total."""
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    budget = state.setdefault("_budget", {"month": month, "spent": 0.0, "next_check": 0})
    if budget["month"] != month:
        budget.update(month=month, spent=0.0)
    budget["spent"] = round(budget["spent"] + cost, 6)
    return budget["spent"]


def post_new(webhook, person, notes, saved):
    new = [n for n in notes if str(n["id"]) not in saved["seen"]]
    print(f"Checked {person['handle']}: {len(new)} new notes.")
    if not saved["started"]:
        saved["seen"] += [str(n["id"]) for n in notes]
        saved["started"] = True
        print(f"First check of {person['handle']}: saved {len(notes)} notes without posting.")
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
    people = load_people()
    records = {}
    for person in people:
        carry_over(state, person["handle"])
        records[person["handle"]] = state.setdefault(person["handle"],
                                                     {"seen": [], "started": False})

    budget = state.setdefault("_budget", {
        "month": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m"), "spent": 0.0,
        "next_check": max((r.get("next_check", 0) for r in records.values()), default=0)})
    if now < budget["next_check"] and all(r["started"] for r in records.values()):
        save_state(state)
        return
    budget["next_check"] = now + CHECK_EVERY_SECONDS

    try:
        seen = [i for r in records.values() for i in r["seen"]]
        since = datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        found, cost = fetch_notes(people, token, seen, since)
        spent = record_cost(state, cost, now)
        budget["next_check"] = now + next_gap(cost, spent, now)
        print(f"Spent ${spent:.2f} of ${MONTHLY_BUDGET:.2f} this month. Next check in "
              f"{round((budget['next_check'] - now) / 60)} minutes.")
        for person in people:
            try:
                post_new(webhook, person, found[person["handle"]], records[person["handle"]])
            except RuntimeError as error:
                print(f"Couldn't post for {person['handle']}: {error}")
                status.note("substack", f"A Substack note couldn't be posted: {error}")
    except RuntimeError as error:
        print(f"Skipped this check: {error}")
        status.note("substack", f"The Substack check failed: {error}")
    finally:
        save_state(state)


def preview(webhook, token):
    """Print or post each person's latest note, using one shared run."""
    state = load_state()
    people = load_people()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    found, cost = fetch_notes(people, token, [], since)
    record_cost(state, cost, time.time())
    for person in people:
        notes = found[person["handle"]]
        if not notes:
            print(f"{person['handle']}: no recent notes found.")
            continue
        card = build_card(notes[0], person)
        print(json.dumps(card, indent=1, ensure_ascii=False))
        if webhook:
            post_to_discord(webhook, "", [card])
            # A test post counts as sent, so a normal run won't post it again.
            saved = state.get(person["handle"])
            if saved is not None and str(notes[0]["id"]) not in saved["seen"]:
                saved["seen"].append(str(notes[0]["id"]))
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

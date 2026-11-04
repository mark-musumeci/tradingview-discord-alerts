#!/usr/bin/env python3
"""Post problems, recoveries and a daily summary to the #bot-status channel.

Runs at the end of every check. It stays quiet unless something needs attention:
a problem alerts on its second sighting, so one-off blips are ignored, then
reminds every 6 hours until it clears. Reads DISCORD_STATUS_WEBHOOK_URL.

  python3 status_report.py                Normal end-of-run report.
  python3 status_report.py --test         Post a status summary now.
  python3 status_report.py --save-failed  Report that saving progress failed.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from status import ISSUES_FILE
from tv_data import EASTERN
from watcher import post_to_discord

HERE = Path(__file__).parent
STATE_FILE = HERE / "status_state.json"
CALENDAR_STATE = HERE / "calendar_state.json"
SUBSTACK_STATE = HERE / "substack_state.json"

STEPS = {"STEP_NEWS": "Top stories check", "STEP_CALENDAR": "Calendar check",
         "STEP_SUBSTACK": "Substack check", "STEP_WEEKLY": "Weekly PDF"}

ALERT_AFTER_SIGHTINGS = 2
REMIND_SECONDS = 6 * 3600
CLEAR_SECONDS = {"ai": 24 * 3600}  # AI is only called when there's something new.
DEFAULT_CLEAR_SECONDS = 3 * 3600
TIMER_GAP_SECONDS = 30 * 60
SUMMARY_HOUR = 8
BRIEF_DEADLINE_HOUR = 12
APIFY_WARNING = 0.8


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def run_link():
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"<https://github.com/{repo}/actions/runs/{run_id}>" if run_id and repo else ""


def run_issues():
    """Return {key: message} for everything that went wrong in this run."""
    issues = {}
    for env, name in STEPS.items():
        if os.environ.get(env) == "failure":
            issues[f"step:{env}"] = f"{name} failed."
    if ISSUES_FILE.exists():
        for line in ISSUES_FILE.read_text().splitlines():
            item = json.loads(line)
            issues.setdefault(item["key"], item["message"])
    return issues


def missing_issues(now):
    """Check that scheduled posts really went out."""
    issues = {}
    calendar = read_json(CALENDAR_STATE, {})
    today = now.date().isoformat()
    if (now.weekday() < 5 and now.hour >= BRIEF_DEADLINE_HOUR
            and calendar.get("morning_date") != today):
        issues[f"brief:{today}"] = "Today's morning brief didn't send."
    friday = (now.date() - timedelta(days=(now.weekday() - 4) % 7))
    if now.weekday() == 5 and calendar.get("weekly_sent") != friday.isoformat():
        issues[f"weekly:{friday}"] = "Friday's weekly PDF didn't send."
    return issues


def alert_text(message):
    return "\n".join(line for line in (f"🔴 **Problem:** {message}", run_link()) if line)


def update_issues(state, found, stamp, post):
    """Alert, remind and clear. Each problem is tracked by its key."""
    tracked = state.setdefault("issues", {})
    for key, message in found.items():
        issue = tracked.setdefault(key, {"count": 0, "alerted": 0, "message": message})
        issue.update(count=issue["count"] + 1, last_seen=stamp, message=message)
        ready = issue["count"] >= ALERT_AFTER_SIGHTINGS or key.startswith(("brief:", "weekly:"))
        if ready and (not issue["alerted"] or stamp - issue["alerted"] >= REMIND_SECONDS):
            post(alert_text(message))
            issue["alerted"] = stamp

    for key in list(tracked):
        issue = tracked[key]
        clear_after = CLEAR_SECONDS.get(key.split(":")[0], DEFAULT_CLEAR_SECONDS)
        if key not in found and stamp - issue["last_seen"] >= clear_after:
            if issue["alerted"]:
                post(f"🟢 **Recovered:** {issue['message']}")
            del tracked[key]


def check_timer(state, stamp, post):
    """Warn if runs stopped for a while. GitHub's backup schedule makes this visible."""
    last = state.get("last_run")
    if last and stamp - last >= TIMER_GAP_SECONDS:
        post(f"🟠 **Gap in checks:** no run for {round((stamp - last) / 60)} minutes. "
             "Check cron-job.org if this repeats.")


def count_run(state, now, failed):
    today = now.date().isoformat()
    if state.get("day") != today:
        state["yesterday"] = {"runs": state.get("runs", 0), "failed": state.get("failed", 0)}
        state.update(day=today, runs=0, failed=0)
    state["runs"] += 1
    state["failed"] += 1 if failed else 0


def summary(state):
    spent = read_json(SUBSTACK_STATE, {}).get("_budget", {}).get("spent", 0)
    past = state.get("yesterday") or {"runs": state.get("runs", 0), "failed": state.get("failed", 0)}
    open_issues = [i["message"] for i in state.get("issues", {}).values() if i["alerted"]]
    lines = ["📊 **Daily status**",
             f"Runs in the last day: {past['runs']}, with {past['failed']} failed",
             f"Apify: ${spent:.2f} of $4.75 this month"]
    lines.append(f"Open problems: {len(open_issues)}" if open_issues else "Open problems: none")
    lines += [f"- {message}" for message in open_issues]
    return "\n".join(lines)


def check_apify(state, month, post):
    budget = read_json(SUBSTACK_STATE, {}).get("_budget", {})
    limit = APIFY_WARNING * 4.75
    if budget.get("spent", 0) >= limit and state.get("apify_warned") != month:
        post(f"🟠 **Apify spending:** ${budget['spent']:.2f} of $4.75 used this month. "
             "Checks will slow down automatically if needed.")
        state["apify_warned"] = month


def run(webhook):
    now = datetime.now(EASTERN)
    stamp = time.time()
    state = read_json(STATE_FILE, {})
    post = lambda text: post_to_discord(webhook, text)

    found = {**run_issues(), **missing_issues(now)}
    count_run(state, now, any(k.startswith("step:") for k in found))
    check_timer(state, stamp, post)
    state["last_run"] = stamp
    update_issues(state, found, stamp, post)
    check_apify(state, now.strftime("%Y-%m"), post)

    if now.hour >= SUMMARY_HOUR and state.get("summary_day") != now.date().isoformat():
        post(summary(state))
        state["summary_day"] = now.date().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=0) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Report the bot's status to Discord.")
    parser.add_argument("--test", action="store_true", help="post a status summary now")
    parser.add_argument("--save-failed", action="store_true", help="report a failed save")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_STATUS_WEBHOOK_URL", "").strip()
    if not webhook:
        print("No status webhook set, so skipping.")
        return
    try:
        if args.save_failed:
            post_to_discord(webhook, alert_text(
                "Progress couldn't be saved to GitHub, so the next run may repeat a post."))
        elif args.test:
            post_to_discord(webhook, summary(read_json(STATE_FILE, {})))
            run(webhook)
            print("Posted a test status.")
        else:
            run(webhook)
    except RuntimeError as error:
        sys.exit(f"Status report failed: {error}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Post a daily brief and live updates to the calendar Discord channel.

Each weekday morning it posts one short brief with the market regime and the
day's main Economic, Earnings, Dividends and IPO items. Through the day it posts
high-importance US and Australian economic results and recaps of big earnings reports as they
come out. Run it on a schedule, every few minutes.

  python3 calendar_watcher.py --dry-run   Print the next brief and a sample recap.
  python3 calendar_watcher.py --test      Post the next brief to Discord.
  python3 calendar_watcher.py             Normal run.

The webhook URL is read from the DISCORD_CALENDAR_WEBHOOK_URL environment
variable. A normal run does nothing until that is set.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import archive
import recaps
import regime
from tv_data import EASTERN, SITE_URL, day_range, fetch_json, large_us_companies, money, scan
from watcher import escape_markdown, post_to_discord

ECONOMIC_URL = "https://economic-calendar.tradingview.com/events"

STATE_FILE = Path(__file__).with_name("calendar_state.json")

MORNING_START_HOUR = 7   # Post the brief from this hour, Eastern...
MORNING_END_HOUR = 12    # ...and skip it if the schedule is this late.
MIN_MARKET_CAP = 10e9    # Earnings and dividends: companies over $10B.
LIMITS = {"economic": 5, "earnings": 5, "dividends": 3, "ipo": 3}
MAX_NAME_CHARS = 30
MAX_POSTED = 500         # Economic result IDs to remember.

RELEASE_TIMES = {-1: "Before open", 1: "After close"}
FLAGS = {"US": "🇺🇸", "AU": "🇦🇺"}
MAX_AUSTRALIA = 2        # Slots kept for Australian events in the brief.


def fetch_economic(day):
    """High-importance events on an Eastern day, in time order."""
    start = datetime(day.year, day.month, day.day, tzinfo=EASTERN)
    end = start + timedelta(days=1)
    stamp = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = urllib.parse.urlencode(
        {"from": stamp(start), "to": stamp(end), "countries": ",".join(FLAGS),
         "minImportance": 1}
    )
    events = fetch_json(f"{ECONOMIC_URL}?{query}").get("result") or []
    events = [e for e in events if e.get("id") and e.get("importance", 1) == 1]
    return sorted(events, key=lambda e: e.get("date", ""))


def number(value, scale="", unit=""):
    if value is None:
        return "n/a"
    unit = unit or ""
    gap = " " if unit[:1].isalpha() else ""
    return f"{value:g}{scale or ''}{gap}{unit}"


def event_time(event):
    moment = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    return moment.astimezone(EASTERN).strftime("%-I:%M %p ET")


def event_name(event):
    period = f" ({event['period']})" if event.get("period") else ""
    return escape_markdown(f"{event.get('title', 'Event')}{period}")


def flag(event):
    return FLAGS.get(event.get("country"), "")


def event_value(event, key):
    return number(event.get(key), event.get("scale"), event.get("unit"))


def short_name(row):
    return escape_markdown((row.get("description") or "")[:MAX_NAME_CHARS].strip())


def ticker(row):
    return escape_markdown(row["symbol"].partition(":")[2])


def pick_events(events):
    """Choose the brief's events: rate decisions first, then scheduled data."""
    limit = LIMITS["economic"]
    ranked = sorted(events, key=lambda e: ("rate decision" not in e.get("title", "").lower(),
                                           e.get("forecast") is None))
    australia = [e for e in ranked if e.get("country") == "AU"][:MAX_AUSTRALIA]
    others = [e for e in ranked if e.get("country") != "AU"][:limit - len(australia)]
    picked = australia + others
    picked += [e for e in ranked if e not in picked][:limit - len(picked)]
    return sorted(picked, key=lambda e: e.get("date", ""))


def economic_lines(events):
    lines = [
        f"{event_time(e)} · {flag(e)} **{event_name(e)}** · "
        f"Fcst {event_value(e, 'forecast')} · Prev {event_value(e, 'previous')}"
        for e in pick_events(events)
    ]
    return lines, len(events)


def earnings_lines(day):
    t0, t1 = day_range(day)
    columns = [
        "description", "market_cap_basic", "earnings_release_date",
        "earnings_release_next_date", "earnings_release_time", "earnings_release_next_time",
        "earnings_per_share_forecast_fq", "earnings_per_share_forecast_next_fq",
    ]
    rows = large_us_companies(
        scan(columns, [{"left": "earnings_release_date,earnings_release_next_date",
                        "operation": "in_range", "right": [t0, t1]}]),
        MIN_MARKET_CAP,
    )
    lines = []
    for row in rows[:LIMITS["earnings"]]:
        last = t0 <= (row.get("earnings_release_date") or 0) <= t1
        pick = lambda recent, upcoming: row.get(recent if last else upcoming)
        when = RELEASE_TIMES.get(pick("earnings_release_time", "earnings_release_next_time"),
                                 "Time not set")
        estimate = pick("earnings_per_share_forecast_fq", "earnings_per_share_forecast_next_fq")
        eps = "n/a" if estimate is None else f"{estimate:.2f}"
        lines.append(f"**{ticker(row)}** {short_name(row)} · {when} · EPS est {eps}")
    return lines, len(rows)


def dividends_lines(day):
    t0, t1 = day_range(day)
    columns = [
        "description", "market_cap_basic", "dividend_ex_date_recent", "dividend_ex_date_upcoming",
        "dividend_amount_recent", "dividend_amount_upcoming",
        "dividends_yield", "fundamental_currency_code",
    ]
    rows = large_us_companies(
        scan(columns, [{"left": "dividend_ex_date_recent,dividend_ex_date_upcoming",
                        "operation": "in_range", "right": [t0, t1]}]),
        MIN_MARKET_CAP,
    )
    lines = []
    for row in rows[:LIMITS["dividends"]]:
        last = t0 <= (row.get("dividend_ex_date_recent") or 0) <= t1
        amount = row.get("dividend_amount_recent" if last else "dividend_amount_upcoming")
        parts = [f"**{ticker(row)}** {short_name(row)}"]
        if amount is not None:
            currency = row.get("fundamental_currency_code") or "USD"
            parts.append(f"${amount:.2f}" if currency == "USD" else f"{amount:.2f} {currency}")
        if row.get("dividends_yield") is not None:
            parts.append(f"Yield {row['dividends_yield']:.2f}%")
        lines.append(" · ".join(parts))
    return lines, len(rows)


def ipo_lines(day):
    t0, t1 = day_range(day)
    columns = ["description", "ipo_offer_time", "ipo_offer_price_usd", "ipo_price_range_usd",
               "ipo_deal_amount_usd"]
    rows = scan(
        columns,
        [{"left": "ipo_offer_time", "operation": "in_range", "right": [t0, t1]},
         {"left": "ipo_offer_time", "operation": "nempty"}],
        sort={"sortBy": "ipo_offer_time", "sortOrder": "asc"},
        preset="ipo_calendar",
    )
    lines = []
    for row in rows[:LIMITS["ipo"]]:
        parts = [f"**{ticker(row)}** {short_name(row)}"]
        price = ipo_price(row)
        if price:
            parts.append(price)
        if row.get("ipo_deal_amount_usd"):
            parts.append(f"Raising {money(row['ipo_deal_amount_usd'])}")
        lines.append(" · ".join(parts))
    return lines, len(rows)


def ipo_price(row):
    offer = row.get("ipo_offer_price_usd")
    if offer:
        return f"${offer:.2f}"
    price_range = row.get("ipo_price_range_usd")
    if isinstance(price_range, (list, tuple)):
        return "–".join(f"${p:g}" for p in price_range if p is not None)
    return escape_markdown(str(price_range)) if price_range else ""


def section(heading, build):
    """Return a section's lines. Empty sections are left out of the brief."""
    try:
        lines, total = build()
    except RuntimeError as error:
        print(f"{heading}: {error}", file=sys.stderr)
        return [f"**{heading}**", "Unavailable right now."]
    if not lines:
        return []
    more = [f"+{total - len(lines)} more"] if total > len(lines) else []
    return [f"**{heading}**", *lines, *more]


def build_brief(day, events, reading=None):
    """Build the single morning post."""
    heading, detail = reading or regime.daily_reading()
    lines = [f"**☀️ Morning brief · {day.strftime('%a, %b')} {day.day}**", heading, detail]
    sections = [
        ("🏛️ Economic", lambda: economic_lines(events)),
        ("💵 Earnings", lambda: earnings_lines(day)),
        ("💰 Dividends (ex-date)", lambda: dividends_lines(day)),
        ("🚀 IPO", lambda: ipo_lines(day)),
    ]
    for name, build in sections:
        lines += section(name, build)
    if len(lines) == 3:
        lines.append("No major events today.")
    footer = f"<{SITE_URL}/economic-calendar/>"
    while len("\n".join([*lines, footer])) > 1900:
        lines.pop()
    return "\n".join([*lines, footer])


def result_message(event):
    return "\n".join([
        f"**🏛️ Economic result: {flag(event)} {event_name(event)}**",
        f"Actual {event_value(event, 'actual')} · Forecast {event_value(event, 'forecast')} · "
        f"Previous {event_value(event, 'previous')}",
        f"<{SITE_URL}/economic-calendar/>",
    ])


def next_weekday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()), False
    return {"morning_date": "", "posted": []}, True


def save_state(state):
    state["posted"] = state["posted"][-MAX_POSTED:]
    STATE_FILE.write_text(json.dumps(state, indent=0) + "\n")


def post_results(webhook, state, events, first_run, now):
    """Post any economic results not seen yet. A first run only records them."""
    released = [e for e in events if e.get("actual") is not None]
    for event in released:
        if event["id"] in state["posted"]:
            continue
        if not first_run:
            post_to_discord(webhook, result_message(event))
            archive.add_result(event, now)
            print(f"Posted result: {event.get('title')}")
            time.sleep(1)
        state["posted"].append(event["id"])


def run(webhook, now):
    state, first_run = load_state()
    day = now.date()
    in_morning = (day.weekday() < 5 and MORNING_START_HOUR <= now.hour < MORNING_END_HOUR)
    events = fetch_economic(day)
    try:
        if in_morning and state.get("morning_date") != day.isoformat():
            inputs = regime.read_inputs()
            reading = regime.summary(*inputs)
            post_to_discord(webhook, build_brief(day, events, reading))
            state["morning_date"] = day.isoformat()
            archive.add_regime(*reading, now, regime.sentiment_score(*inputs))
            print("Posted the morning brief.")
        post_results(webhook, state, events, first_run, now)
        recaps.post_recaps(lambda text: post_to_discord(webhook, text), state, now, first_run)
    finally:
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Post TradingView calendars to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="print the posts only")
    parser.add_argument("--test", action="store_true", help="post them to Discord now")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_CALENDAR_WEBHOOK_URL", "").strip()
    now = datetime.now(EASTERN)
    try:
        day = next_weekday(now.date())
        if args.dry_run:
            print(build_brief(day, fetch_economic(day)) + "\n")
            print(recaps.preview(now))
        elif args.test:
            if not webhook:
                sys.exit("Set the DISCORD_CALENDAR_WEBHOOK_URL environment variable first.")
            post_to_discord(webhook, build_brief(day, fetch_economic(day)))
            print("Posted the morning brief.")
        elif webhook:
            run(webhook, now)
        else:
            print("No calendar webhook set, so skipping.")
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

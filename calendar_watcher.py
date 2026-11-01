#!/usr/bin/env python3
"""Post TradingView calendar updates to a Discord channel.

Each weekday morning it posts one labelled message each for Economic, Earnings,
Dividends and IPO. Through the day it also posts high-importance US economic
results as they are released. Run it on a schedule, every few minutes.

  python3 calendar_watcher.py --dry-run   Print the next weekday's posts only.
  python3 calendar_watcher.py --test      Post them to Discord, to check it.
  python3 calendar_watcher.py             Normal run.

The webhook URL is read from the DISCORD_CALENDAR_WEBHOOK_URL environment
variable. A normal run does nothing until that is set.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from watcher import SITE_URL, TV_HEADERS, escape_markdown, post_to_discord

ECONOMIC_URL = "https://economic-calendar.tradingview.com/events"
SCAN_URL = "https://scanner.tradingview.com/global/scan"

STATE_FILE = Path(__file__).with_name("calendar_state.json")
EASTERN = ZoneInfo("America/New_York")

MORNING_START_HOUR = 7   # Post the morning updates from this hour, Eastern...
MORNING_END_HOUR = 12    # ...and skip them if the schedule is this late.
MIN_MARKET_CAP = 10e9    # Earnings and dividends: companies over $10B.
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
MAX_LINES = 15           # Most rows in one post.
MAX_POSTED = 500         # Economic result IDs to remember.
TIMEOUT = 60
ATTEMPTS = 3

RELEASE_TIMES = {-1: "Before open", 1: "After close"}


def fetch_json(url, payload=None):
    headers = dict(TV_HEADERS)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)

    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            if attempt == ATTEMPTS:
                raise RuntimeError(f"TradingView request failed: {error}") from error
            time.sleep(2 ** attempt)


def fetch_economic(day):
    """High-importance US events on an Eastern day, in time order."""
    start = datetime(day.year, day.month, day.day, tzinfo=EASTERN)
    end = start + timedelta(days=1)
    stamp = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = urllib.parse.urlencode(
        {"from": stamp(start), "to": stamp(end), "countries": "US", "minImportance": 1}
    )
    events = fetch_json(f"{ECONOMIC_URL}?{query}").get("result") or []
    events = [e for e in events if e.get("id") and e.get("importance", 1) == 1]
    return sorted(events, key=lambda e: e.get("date", ""))


def scan(columns, filters, **extra):
    """Run a calendar query and return one dict per row."""
    payload = {
        "columns": columns, "filter": filters, "markets": ["america"],
        "options": {"lang": "en"}, "range": [0, 5000], **extra,
    }
    rows = fetch_json(SCAN_URL, payload).get("data") or []
    return [{"symbol": r["s"], **dict(zip(columns, r["d"]))} for r in rows]


def day_range(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start, start + 86400 - 0.001


def large_us_companies(rows):
    """Keep large US-listed companies, biggest first, one line per company."""
    keep = {}
    rows = sorted(rows, key=lambda r: r.get("market_cap_basic") or 0, reverse=True)
    for row in rows:
        exchange, _, ticker = row["symbol"].partition(":")
        name = row.get("description") or ticker
        if (exchange in US_EXCHANGES and "/" not in ticker and name not in keep
                and (row.get("market_cap_basic") or 0) >= MIN_MARKET_CAP):
            keep[name] = row
    return list(keep.values())


def number(value, scale="", unit=""):
    if value is None:
        return "n/a"
    unit = unit or ""
    gap = " " if unit[:1].isalpha() else ""
    return f"{value:g}{scale or ''}{gap}{unit}"


def money(value):
    if value is None:
        return "n/a"
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= size:
            return f"${value / size:.1f}{suffix}"
    return f"${value:,.2f}"


def event_time(event):
    moment = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    return moment.astimezone(EASTERN).strftime("%-I:%M %p ET")


def event_name(event):
    period = f" ({event['period']})" if event.get("period") else ""
    return escape_markdown(f"{event.get('title', 'Event')}{period}")


def event_value(event, key):
    return number(event.get(key), event.get("scale"), event.get("unit"))


def post_text(label, day, lines, page, empty):
    """Build one labelled post, trimming the list to fit Discord's limit."""
    heading = f"**{label} · {day.strftime('%a, %b')} {day.day}**"
    footer = f"<{SITE_URL}/{page}/>"
    if not lines:
        return "\n".join([heading, empty, footer])

    shown = lines[:MAX_LINES]
    while True:
        extra = len(lines) - len(shown)
        more = [f"+{extra} more"] if extra else []
        text = "\n".join([heading, *shown, *more, footer])
        if len(text) <= 1900:
            return text
        shown.pop()


def economic_post(day, events):
    lines = [
        f"{event_time(e)} · **{event_name(e)}** · "
        f"Forecast {event_value(e, 'forecast')} · Previous {event_value(e, 'previous')}"
        for e in events
    ]
    return post_text("🏛️ Economic", day, lines, "economic-calendar",
                     "No high-importance US events.")


def earnings_post(day):
    t0, t1 = day_range(day)
    columns = [
        "description", "market_cap_basic", "earnings_release_date",
        "earnings_release_next_date", "earnings_release_time", "earnings_release_next_time",
        "earnings_per_share_forecast_fq", "earnings_per_share_forecast_next_fq",
        "revenue_forecast_fq", "revenue_forecast_next_fq",
    ]
    rows = scan(columns, [{"left": "earnings_release_date,earnings_release_next_date",
                           "operation": "in_range", "right": [t0, t1]}])
    lines = []
    for row in large_us_companies(rows):
        last = t0 <= (row.get("earnings_release_date") or 0) <= t1
        pick = lambda recent, upcoming: row.get(recent if last else upcoming)
        when = RELEASE_TIMES.get(pick("earnings_release_time", "earnings_release_next_time"),
                                 "Time not set")
        eps = pick("earnings_per_share_forecast_fq", "earnings_per_share_forecast_next_fq")
        revenue = pick("revenue_forecast_fq", "revenue_forecast_next_fq")
        lines.append(
            f"**{ticker(row)}** {escape_markdown(row.get('description') or '')} · {when} · "
            f"EPS est {'n/a' if eps is None else f'{eps:.2f}'} · Rev est {money(revenue)}"
        )
    return post_text("💵 Earnings", day, lines, "earnings-calendar",
                     "No companies over $10B reporting.")


def dividends_post(day):
    t0, t1 = day_range(day)
    columns = [
        "description", "market_cap_basic", "dividend_ex_date_recent", "dividend_ex_date_upcoming",
        "dividend_payment_date_recent", "dividend_payment_date_upcoming",
        "dividend_amount_recent", "dividend_amount_upcoming",
        "dividends_yield", "fundamental_currency_code",
    ]
    rows = scan(columns, [{"left": "dividend_ex_date_recent,dividend_ex_date_upcoming",
                           "operation": "in_range", "right": [t0, t1]}])
    lines = []
    for row in large_us_companies(rows):
        last = t0 <= (row.get("dividend_ex_date_recent") or 0) <= t1
        pick = lambda recent, upcoming: row.get(recent if last else upcoming)
        amount = pick("dividend_amount_recent", "dividend_amount_upcoming")
        paid = pick("dividend_payment_date_recent", "dividend_payment_date_upcoming")
        yield_pct = row.get("dividends_yield")

        parts = [f"**{ticker(row)}** {escape_markdown(row.get('description') or '')}"]
        if amount is not None:
            currency = row.get("fundamental_currency_code") or "USD"
            parts.append(f"${amount:.2f}" if currency == "USD" else f"{amount:.2f} {currency}")
        if yield_pct is not None:
            parts.append(f"Yield {yield_pct:.2f}%")
        if paid:
            pay_date = datetime.fromtimestamp(paid, timezone.utc)
            parts.append(f"Pays {pay_date.strftime('%b')} {pay_date.day}")
        lines.append(" · ".join(parts))
    return post_text("💰 Dividends (ex-date)", day, lines, "dividend-calendar",
                     "No companies over $10B going ex-dividend.")


def ipo_post(day):
    t0, t1 = day_range(day)
    columns = [
        "description", "exchange", "ipo_offer_time", "ipo_offer_status", "ipo_offer_price_usd",
        "ipo_price_range_usd", "ipo_deal_amount_usd",
    ]
    rows = scan(
        columns,
        [{"left": "ipo_offer_time", "operation": "in_range", "right": [t0, t1]},
         {"left": "ipo_offer_time", "operation": "nempty"}],
        sort={"sortBy": "ipo_offer_time", "sortOrder": "asc"},
        preset="ipo_calendar",
    )
    lines = []
    for row in rows:
        parts = [f"**{ticker(row)}** {escape_markdown(row.get('description') or '')}"]
        if row.get("exchange"):
            parts.append(escape_markdown(str(row["exchange"])))
        price = ipo_price(row)
        if price:
            parts.append(price)
        if row.get("ipo_deal_amount_usd"):
            parts.append(f"Raising {money(row['ipo_deal_amount_usd'])}")
        if row.get("ipo_offer_status"):
            parts.append(escape_markdown(str(row["ipo_offer_status"]).replace("_", " ").title()))
        lines.append(" · ".join(parts))
    return post_text("🚀 IPO", day, lines, "ipo-calendar", "No US IPOs.")


def ipo_price(row):
    offer = row.get("ipo_offer_price_usd")
    if offer:
        return f"${offer:.2f}"
    price_range = row.get("ipo_price_range_usd")
    if isinstance(price_range, (list, tuple)):
        return "–".join(f"${p:g}" for p in price_range if p is not None)
    return escape_markdown(str(price_range)) if price_range else ""


def ticker(row):
    return escape_markdown(row["symbol"].partition(":")[2])


def result_message(event):
    return "\n".join([
        f"**🏛️ Economic result: {event_name(event)}**",
        f"Actual {event_value(event, 'actual')} · Forecast {event_value(event, 'forecast')} · "
        f"Previous {event_value(event, 'previous')}",
        f"<{SITE_URL}/economic-calendar/>",
    ])


def morning_posts(day, events):
    """Return (name, build function) for each morning post."""
    return [
        ("economic", lambda: economic_post(day, events)),
        ("earnings", lambda: earnings_post(day)),
        ("dividends", lambda: dividends_post(day)),
        ("ipo", lambda: ipo_post(day)),
    ]


def next_weekday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()), False
    return {"morning_date": "", "morning_done": [], "posted": []}, True


def save_state(state):
    state["posted"] = state["posted"][-MAX_POSTED:]
    STATE_FILE.write_text(json.dumps(state, indent=0) + "\n")


def post_morning(webhook, state, day, events):
    """Post today's morning updates, resuming if an earlier run stopped partway."""
    if state["morning_date"] != day.isoformat():
        state["morning_date"], state["morning_done"] = day.isoformat(), []

    failed = False
    for name, build in morning_posts(day, events):
        if name in state["morning_done"]:
            continue
        try:
            post_to_discord(webhook, build())
        except RuntimeError as error:
            print(f"{name}: {error}", file=sys.stderr)
            failed = True
            continue
        state["morning_done"].append(name)
        print(f"Posted the {name} update.")
        time.sleep(1)
    return not failed


def post_results(webhook, state, events, first_run):
    """Post any economic results not seen yet. A first run only records them."""
    released = [e for e in events if e.get("actual") is not None]
    for event in released:
        if event["id"] in state["posted"]:
            continue
        if not first_run:
            post_to_discord(webhook, result_message(event))
            print(f"Posted result: {event.get('title')}")
            time.sleep(1)
        state["posted"].append(event["id"])


def run(webhook, now):
    state, first_run = load_state()
    day = now.date()
    in_morning = (day.weekday() < 5 and MORNING_START_HOUR <= now.hour < MORNING_END_HOUR)
    events = fetch_economic(day)
    ok = True
    try:
        if in_morning:
            ok = post_morning(webhook, state, day, events)
        post_results(webhook, state, events, first_run)
    finally:
        save_state(state)
    if not ok:
        raise RuntimeError("At least one morning update failed. It will retry on the next run.")


def preview(webhook, day):
    """Print the morning posts, or send them when a webhook is given."""
    events = fetch_economic(day)
    for name, build in morning_posts(day, events):
        text = build()
        if webhook:
            post_to_discord(webhook, text)
            print(f"Posted the {name} update.")
            time.sleep(1)
        else:
            print(text + "\n")


def main():
    parser = argparse.ArgumentParser(description="Post TradingView calendars to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="print the posts only")
    parser.add_argument("--test", action="store_true", help="post them to Discord now")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_CALENDAR_WEBHOOK_URL", "").strip()
    now = datetime.now(EASTERN)
    try:
        if args.dry_run:
            preview("", next_weekday(now.date()))
        elif args.test:
            if not webhook:
                sys.exit("Set the DISCORD_CALENDAR_WEBHOOK_URL environment variable first.")
            preview(webhook, next_weekday(now.date()))
        elif webhook:
            run(webhook, now)
        else:
            print("No calendar webhook set, so skipping.")
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

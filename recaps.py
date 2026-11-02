"""Short recaps after big earnings reports.

Beat or miss always comes from reported figures, worked out here. Only the
guidance and "stood out" lines use AI, and each is dropped unless it passes the
source checks in ai.py.
"""
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import ai
from tv_data import SITE_URL, day_range, large_us_companies, money, scan
from watcher import NEWS_URL, escape_markdown, fetch_json, story_text, story_url

MIN_CAP = 50e9        # "Big" reports: companies over $50B.
WAIT_MINUTES = 30     # How long to wait for an article before posting figures alone.
MAX_ARTICLES = 3
MAX_SOURCE_CHARS = 9000
MAX_RECAPS_PER_RUN = 5

COLUMNS = [
    "description", "market_cap_basic", "earnings_release_date", "earnings_per_share_fq",
    "earnings_per_share_forecast_fq", "eps_surprise_percent_fq", "revenue_fq",
    "revenue_forecast_fq", "revenue_surprise_percent_fq",
]


def reported_between(start, end):
    """Big companies that reported between two dates, biggest first."""
    rows = scan(COLUMNS, [{"left": "earnings_release_date", "operation": "in_range",
                           "right": [day_range(start)[0], day_range(end)[1]]}])
    return [r for r in large_us_companies(rows, MIN_CAP)
            if r.get("earnings_per_share_fq") is not None]


def earnings_articles(symbol, since):
    """Recent earnings stories for a company, as (story, full text) pairs."""
    filters = sorted(["corp_activity:earnings", "lang:en", f"symbol:{symbol}"])
    query = urllib.parse.urlencode(
        {"filter": filters, "client": "screener", "streaming": "false",
         "user_prostatus": "non_pro"},
        doseq=True, safe=":",
    )
    try:
        items = fetch_json(f"{NEWS_URL}?{query}").get("items", [])
    except RuntimeError:
        return []
    items = [i for i in items if i.get("id") and i.get("title")
             and i.get("published", 0) >= since][:MAX_ARTICLES]
    return [(item, f"{item['title']}\n{story_text(item['id'])}") for item in items]


def verdict(surprise):
    if surprise is None:
        return "No estimate to compare"
    if surprise > 0:
        return f"✅ Beat ({surprise:+.1f}%)"
    if surprise < 0:
        return f"❌ Miss ({surprise:+.1f}%)"
    return "➖ In line"


def eps(value):
    return "n/a" if value is None else f"{value:.2f}"


def verified_claims(articles):
    """Return the checked AI lines and the story they came from."""
    source = "\n\n".join(text for _, text in articles)[:MAX_SOURCE_CHARS]
    if not source or not ai.enabled():
        return {}, None
    result = ai.ask_json(ai.RECAP_PROMPT, source)
    time.sleep(ai.PACE_SECONDS)
    claims = {key: ai.check_claim(result, key, source) for key in ("guidance", "highlight")}
    claims = {key: value for key, value in claims.items() if value}
    if not claims:
        return {}, None
    quote = ai.normalize(next(iter(claims.values()))[1])
    story = next((s for s, text in articles if quote in ai.normalize(text)), None)
    return claims, story


def recap_message(row, claims, story):
    ticker = escape_markdown(row["symbol"].partition(":")[2])
    name = escape_markdown(row.get("description") or "")
    lines = [
        f"**💵 Earnings recap: {ticker} · {name}**",
        f"EPS {eps(row.get('earnings_per_share_fq'))} vs "
        f"{eps(row.get('earnings_per_share_forecast_fq'))} est · "
        f"{verdict(row.get('eps_surprise_percent_fq'))}",
        f"Revenue {money(row.get('revenue_fq'))} vs {money(row.get('revenue_forecast_fq'))} est · "
        f"{verdict(row.get('revenue_surprise_percent_fq'))}",
    ]
    labels = {"guidance": "Guidance", "highlight": "Stood out"}
    for key, heading in labels.items():
        if key in claims:
            lines.append(f"{heading}: {escape_markdown(claims[key][0])}")
    for key in labels:
        if key in claims:
            lines.append(f'-# Source text: "{escape_markdown(claims[key][1])}"')
    link = story_url(story) if story else f"{SITE_URL}/earnings-calendar/"
    lines.append(f"<{link}>")
    return "\n".join(lines)


def post_recaps(post, state, now, first_run):
    """Post recaps for today's big reports. A first run only records them."""
    day = now.date()
    start = day_range(day)[0]
    recaps = {k: v for k, v in state.get("recaps", {}).items() if k.endswith(day.isoformat())}
    state["recaps"] = recaps
    posted = 0

    for row in reported_between(day, day):
        key = f"{row['symbol']}:{day.isoformat()}"
        if recaps.get(key) == "done":
            continue
        if first_run:
            recaps[key] = "done"
            continue
        if posted >= MAX_RECAPS_PER_RUN:
            break

        articles = earnings_articles(row["symbol"], start)
        first_seen = datetime.fromisoformat(recaps.setdefault(key, now.isoformat()))
        if not articles and now - first_seen < timedelta(minutes=WAIT_MINUTES):
            continue

        claims, story = verified_claims(articles)
        post(recap_message(row, claims, story))
        recaps[key] = "done"
        posted += 1
        print(f"Posted recap: {row['symbol']}")
        time.sleep(1)


def preview(now):
    """Build a recap from the biggest report in the past week, for checking."""
    rows = reported_between(now.date() - timedelta(days=7), now.date())
    if not rows:
        return "No reports over $50B in the past week."
    row = rows[0]
    since = datetime.now(timezone.utc).timestamp() - 8 * 86400
    claims, story = verified_claims(earnings_articles(row["symbol"], since))
    return recap_message(row, claims, story)

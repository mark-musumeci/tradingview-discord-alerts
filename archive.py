"""A two-week record of what was posted to the calendar channel.

The weekly PDF is built from this file. Each item is stored with its exact
figures, so the summary uses what was reported at the time.
"""
import json
from datetime import timedelta
from pathlib import Path

ARCHIVE_FILE = Path(__file__).with_name("weekly_archive.json")
KEEP_DAYS = 14


def load():
    if not ARCHIVE_FILE.exists():
        return []
    return json.loads(ARCHIVE_FILE.read_text())


def add(kind, data, now):
    """Save one posted item. Items older than two weeks are dropped."""
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    items = [i for i in load() if i["at"] >= cutoff]
    items.append({"kind": kind, "at": now.isoformat(), **data})
    ARCHIVE_FILE.write_text(json.dumps(items, indent=0, ensure_ascii=False) + "\n")


def add_result(event, now):
    fields = ("id", "title", "period", "country", "actual", "forecast", "previous",
              "unit", "scale", "date")
    add("result", {key: event.get(key) for key in fields}, now)


def add_regime(heading, detail, now, score=None):
    add("regime", {"heading": heading, "detail": detail, "score": score}, now)


def add_recap(row, claims, story_link, now):
    fields = ("symbol", "description", "market_cap_basic", "earnings_per_share_fq",
              "earnings_per_share_forecast_fq", "eps_surprise_percent_fq", "revenue_fq",
              "revenue_forecast_fq", "revenue_surprise_percent_fq")
    data = {key: row.get(key) for key in fields}
    data["claims"] = {key: list(value) for key, value in claims.items()}
    data["link"] = story_link
    add("recap", data, now)

"""Temporary check: compare TradingView feed settings to see which one is current."""
import urllib.parse
from datetime import datetime, timezone

from tv_data import fetch_json
from watcher import NEWS_URL

BASE = ["lang:en", "market:stock", "market_country:US"]
VARIANTS = [("top_stories", "screener"), ("top_stories", "landing"), ("top_stories", "overview"),
            ("important", "screener"), (None, "screener")]

for priority, client in VARIANTS:
    filters = sorted(BASE + ([f"priority:{priority}"] if priority else []))
    query = urllib.parse.urlencode(
        {"filter": filters, "client": client, "streaming": "false", "user_prostatus": "non_pro"},
        doseq=True, safe=":")
    try:
        items = fetch_json(f"{NEWS_URL}?{query}").get("items", [])
        newest = max(items, key=lambda i: i.get("published", 0))
        moment = datetime.fromtimestamp(newest.get("published", 0), timezone.utc)
        print(f"PROBE {priority} {client}: {len(items)} items, newest {moment:%m-%d %H:%M} UTC, "
              f"{newest.get('title', '')[:60]}")
    except (RuntimeError, ValueError) as error:
        print(f"PROBE {priority} {client}: failed, {error}")

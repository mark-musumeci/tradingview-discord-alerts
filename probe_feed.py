"""Temporary check: compare TradingView feed settings to see which one is current."""
import urllib.parse
from datetime import datetime, timezone

from tv_data import fetch_json
from watcher import NEWS_URL

VARIANTS = [
    (["lang:en", "priority:top_stories"], "screener"),
    (["lang:en", "market:stock", "priority:top_stories"], "screener"),
    (["lang:en", "market_country:US", "priority:top_stories"], "screener"),
    (["lang:en", "market_country:us", "market:stock", "priority:top_stories"], "screener"),
    (["lang:en", "market:stock", "market_country:US", "priority:key_facts"], "screener"),
    (["lang:en", "market:stock", "market_country:US", "priority:flash"], "screener"),
    (["lang:en", "market:stock", "market_country:US", "priority:important"], "screener"),
    (["lang:en", "market:stock", "market_country:US", "priority:top_stories"], "web"),
    (["lang:en", "priority:top_stories"], "landing"),
]

for filters, client in VARIANTS:
    query = urllib.parse.urlencode(
        {"filter": sorted(filters), "client": client, "streaming": "false",
         "user_prostatus": "non_pro"},
        doseq=True, safe=":")
    name = " ".join(f.split(":", 1)[1] for f in sorted(filters)[1:])
    try:
        items = fetch_json(f"{NEWS_URL}?{query}").get("items", [])
        items.sort(key=lambda i: i.get("published", 0), reverse=True)
        print(f"PROBE [{name}] {client}: {len(items)} items")
        for item in items[:4]:
            moment = datetime.fromtimestamp(item.get("published", 0), timezone.utc)
            source = (item.get("provider") or {}).get("name", "")
            print(f"PROBE   {moment:%m-%d %H:%M} {source}: {item.get('title', '')[:90]}")
    except (RuntimeError, ValueError) as error:
        print(f"PROBE [{name}] {client}: failed, {error}")

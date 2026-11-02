"""Shared helpers for reading TradingView data."""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SITE_URL = "https://www.tradingview.com"
SCANNER_BASE = "https://scanner.tradingview.com"
EASTERN = ZoneInfo("America/New_York")
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
TIMEOUT = 60
ATTEMPTS = 3

TV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/",
    "Cache-Control": "no-cache",
}


def fetch_json(url, payload=None):
    """GET a URL, or POST JSON when a payload is given. Retries, then raises."""
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


def rows_from(result, columns):
    rows = result.get("data") or []
    return [{"symbol": r["s"], **dict(zip(columns, r["d"]))} for r in rows]


def scan(columns, filters, scanner="global", **extra):
    """Run a US market query and return one dict per row."""
    payload = {
        "columns": columns, "filter": filters, "markets": ["america"],
        "options": {"lang": "en"}, "range": [0, 5000], **extra,
    }
    return rows_from(fetch_json(f"{SCANNER_BASE}/{scanner}/scan", payload), columns)


def lookup(scanner, tickers, columns):
    """Read specific symbols, such as TVC:VIX, from one of TradingView's scanners."""
    payload = {
        "symbols": {"query": {"types": []}, "tickers": tickers},
        "columns": columns, "range": [0, len(tickers)],
    }
    return rows_from(fetch_json(f"{SCANNER_BASE}/{scanner}/scan", payload), columns)


def day_range(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start, start + 86400 - 0.001


def large_us_companies(rows, min_cap):
    """Keep large US-listed companies, biggest first, one row per company."""
    keep = {}
    rows = sorted(rows, key=lambda r: r.get("market_cap_basic") or 0, reverse=True)
    for row in rows:
        exchange, _, ticker = row["symbol"].partition(":")
        name = row.get("description") or ticker
        if (exchange in US_EXCHANGES and "/" not in ticker and name not in keep
                and (row.get("market_cap_basic") or 0) >= min_cap):
            keep[name] = row
    return list(keep.values())


def money(value):
    if value is None:
        return "n/a"
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= size:
            return f"${value / size:.1f}{suffix}"
    return f"${value:,.2f}"

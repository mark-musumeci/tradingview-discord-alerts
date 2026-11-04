"""A daily risk-on or risk-off reading, built by fixed rules with no AI.

Each of the four inputs scores +1, 0 or -1, and the total sets the reading.
The thresholds are simple rules of thumb, not a tested trading signal.
"""
import status
from tv_data import large_us_companies, lookup, scan

MIN_CAP = 10e9
MIN_COMPANIES = 100
MIN_INPUTS = 3


def score_vix(vix):
    return 1 if vix < 18 else -1 if vix > 25 else 0


def score_curve(spread):
    return 1 if spread > 0.25 else -1 if spread < 0 else 0


def score_breadth(percent):
    return 1 if percent >= 60 else -1 if percent < 40 else 0


def score_trend(close, sma50, sma200):
    above = (close > sma50, close > sma200)
    return 1 if all(above) else -1 if not any(above) else 0


def label(total):
    if total >= 2:
        return "🟢", "Risk-on"
    if total <= -2:
        return "🔴", "Risk-off"
    return "🟡", "Neutral"


def read_vix():
    rows = lookup("cfd", ["TVC:VIX", "CBOE:VIX"], ["close"])
    return next((r["close"] for r in rows if r.get("close") is not None), None)


def read_curve():
    rows = lookup("bonds", ["TVC:US02Y", "TVC:US10Y"], ["close"])
    yields = {r["symbol"]: r.get("close") for r in rows}
    two, ten = yields.get("TVC:US02Y"), yields.get("TVC:US10Y")
    return None if two is None or ten is None else ten - two


def read_trend():
    rows = lookup("america", ["AMEX:SPY"], ["close", "SMA50", "SMA200"])
    if rows and all(rows[0].get(c) is not None for c in ("close", "SMA50", "SMA200")):
        return rows[0]["close"], rows[0]["SMA50"], rows[0]["SMA200"]
    return None


def read_breadth():
    """Percent of US companies over $10B trading above their 200-day average."""
    rows = scan(
        ["description", "market_cap_basic", "close", "SMA200"],
        [{"left": "market_cap_basic", "operation": "egreater", "right": MIN_CAP},
         {"left": "type", "operation": "equal", "right": "stock"}],
        sort={"sortBy": "market_cap_basic", "sortOrder": "desc"},
    )
    rows = [r for r in large_us_companies(rows, MIN_CAP)
            if r.get("close") is not None and r.get("SMA200") is not None]
    if len(rows) < MIN_COMPANIES:
        return None
    return 100 * sum(r["close"] > r["SMA200"] for r in rows) / len(rows)


def safe(read):
    try:
        return read()
    except RuntimeError as error:
        print(f"Regime input unavailable: {error}")
        status.note(f"regime:{read.__name__}", f"A regime input is unavailable ({read.__name__}).")
        return None


def summary(vix, curve, breadth, trend):
    """Return (heading, detail) from the inputs. Missing inputs are skipped."""
    scores, parts = [], []
    if vix is not None:
        scores.append(score_vix(vix))
        parts.append(f"VIX {vix:.1f}")
    if curve is not None:
        scores.append(score_curve(curve))
        parts.append(f"10Y-2Y {curve:+.2f}")
    if breadth is not None:
        scores.append(score_breadth(breadth))
        parts.append(f"Breadth {breadth:.0f}%")
    if trend is not None:
        scores.append(score_trend(*trend))
        above = [name for name, sma in (("50", trend[1]), ("200", trend[2])) if trend[0] > sma]
        parts.append(f"S&P above {' & '.join(above)}-day" if above else "S&P below 50 & 200-day")

    if len(scores) < MIN_INPUTS:
        return "⚪ Regime: unavailable", "Not enough market data today."
    total = sum(scores)
    icon, name = label(total)
    return f"{icon} Regime: {name} ({total:+d}/{len(scores)})", " · ".join(parts)


def scale(value, low, high):
    """Place a value on a 0 to 100 range, capped at both ends."""
    return max(0.0, min(100.0, 100 * (value - low) / (high - low)))


def sentiment_score(vix, curve, breadth, trend):
    """A 0 to 100 reading, from fear to greed, averaging the four inputs equally.

    Each range is a rule of thumb. Returns None with fewer than three inputs.
    """
    parts = []
    if vix is not None:
        parts.append(100 - scale(vix, 12, 35))
    if curve is not None:
        parts.append(scale(curve, -0.5, 1.0))
    if breadth is not None:
        parts.append(scale(breadth, 25, 75))
    if trend is not None:
        parts.append(scale(100 * (trend[0] / trend[2] - 1), -10, 10))
    return round(sum(parts) / len(parts)) if len(parts) >= MIN_INPUTS else None


def read_inputs():
    return safe(read_vix), safe(read_curve), safe(read_breadth), safe(read_trend)


def daily_reading():
    return summary(*read_inputs())

#!/usr/bin/env python3
"""Send a one-page PDF of the week's MarketCal items to Discord.

Runs on the regular schedule and sends once, from 5 PM ET on Friday. Figures come
from the saved items in weekly_archive.json. Gemini only writes the takeaways,
and each one must pass the source checks in ai.py. If Gemini is down, the bot
keeps trying until 6 PM ET, then sends the PDF without takeaways.

  python3 weekly_report.py --check    Exit 0 if a PDF should be built now.
  python3 weekly_report.py --test     Send a PDF of this week so far.
  python3 weekly_report.py --save F   Write the PDF to a file only.
  python3 weekly_report.py            Normal run.

The webhook URL is read from the DISCORD_WEEKLY_WEBHOOK_URL environment variable.
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import ai
import archive
import calendar_watcher as cal
import recaps
import status
from tv_data import EASTERN, day_range, large_us_companies, money, scan
from watcher import retry_delay

SEND_HOUR = 17          # Friday, Eastern.
AI_DEADLINE_HOUR = 18   # After this, send without takeaways.
SEND_WEEKDAY = 4
MAX_RESULTS = 7
MAX_EARNINGS = 7
MAX_AHEAD = 5
MAX_TAKEAWAYS = 3
POST_ATTEMPTS = 5
PAGE_COLOUR = "#22252f"
ASSETS = Path(__file__).with_name("assets")
FLAG_WIDTH, FLAG_HEIGHT = 12, 9
RESULT_COLOURS = {"Beat": "#4ade80", "Miss": "#ff6b6b"}

COUNTRIES = {"US": "US", "AU": "AU"}

TAKEAWAY_PROMPT = ai.RULES + (
    ' SOURCE lists the week\'s economic results and earnings. Return {"takeaways": [up to 3 '
    'items of {"text": one plain sentence of at most 25 words on a main theme or the '
    'biggest surprise, "quote": a full line copied word for word from SOURCE}]}. '
    "Don't predict, and don't give trading advice."
)


def week_of(day):
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=4)


def week_items(day):
    monday, friday = week_of(day)
    return [i for i in archive.load()
            if monday.isoformat() <= i["at"][:10] <= friday.isoformat()]


def value(item, key):
    return cal.number(item.get(key), item.get("scale"), item.get("unit"))


def surprise(item):
    """How far a result landed from forecast, or None if it can't be compared."""
    actual, forecast = item.get("actual"), item.get("forecast")
    if actual is None or forecast is None:
        return None
    return actual - forecast


def result_name(item):
    period = f" ({item['period']})" if item.get("period") else ""
    return f"{COUNTRIES.get(item.get('country'), '')} {item.get('title', 'Event')}{period}".strip()


def top_results(items):
    """Rate decisions first, then the largest misses against forecast."""
    results = [i for i in items if i["kind"] == "result"]
    def rank(item):
        gap = surprise(item)
        forecast = abs(item.get("forecast") or 0) or 1
        size = 0 if gap is None else abs(gap) / forecast
        return ("rate decision" not in (item.get("title") or "").lower(), -size)
    return sorted(results, key=rank)[:MAX_RESULTS]


def top_earnings(items):
    recaps = [i for i in items if i["kind"] == "recap"]
    return sorted(recaps, key=lambda r: r.get("market_cap_basic") or 0, reverse=True)[:MAX_EARNINGS]


def verdict(percent):
    if percent is None:
        return "n/a"
    label = "Beat" if percent > 0 else "Miss" if percent < 0 else "In line"
    return f"{label} {percent:+.1f}%"


def with_flag(text):
    """Put a flag image before the first US or AU in text. PDF fonts can't show emoji."""
    def swap(match):
        code = match.group(1)
        path = ASSETS / f"flag-{code.lower()}.png"
        return (f'<img src="{path}" width="{FLAG_WIDTH}" height="{FLAG_HEIGHT}" valign="-1.5"/>'
                f"&nbsp;{code} ")
    return re.sub(r"\b(US|AU) ", swap, clean(text), count=1)


def coloured(text):
    """Colour a result green for a beat and red for a miss. Others stay as they are."""
    colour = RESULT_COLOURS.get(text.split(" ")[0])
    return f'<font color="{colour}">{clean(text)}</font>' if colour else clean(text)


def source_lines(results, earnings):
    """The week's facts as plain lines. Takeaways must quote from these."""
    lines = []
    for item in results:
        lines.append(f"{result_name(item)}: actual {value(item, 'actual')}, "
                     f"forecast {value(item, 'forecast')}, previous {value(item, 'previous')}")
    for item in earnings:
        name = item["symbol"].partition(":")[2]
        lines.append(f"{name} earnings: EPS {verdict(item.get('eps_surprise_percent_fq'))}, "
                     f"revenue {verdict(item.get('revenue_surprise_percent_fq'))}")
        for text, _quote in (item.get("claims") or {}).values():
            lines.append(f"{name}: {text}")
    return lines


def takeaways(lines):
    """Return verified takeaways, or None if the AI gave no usable reply."""
    if not lines or not ai.enabled():
        return None
    source = "\n".join(lines)
    result = ai.ask_json(TAKEAWAY_PROMPT, source)
    if not isinstance(result, dict) or not isinstance(result.get("takeaways"), list):
        return None
    checked = []
    for item in result["takeaways"][:MAX_TAKEAWAYS]:
        if isinstance(item, dict) and ai.claim_ok(item.get("text"), item.get("quote"), source):
            checked.append(item["text"].strip())
        else:
            print("Dropped a takeaway that failed the source check.")
    return checked


def week_ahead(day):
    """The next week's rate decisions, data and biggest earnings, from live calendars."""
    monday = week_of(day)[0] + timedelta(days=7)
    rows, seen = [], set()
    try:
        for offset in range(5):
            date = monday + timedelta(days=offset)
            for event in cal.pick_events(cal.fetch_economic(date)):
                if event["id"] not in seen:
                    seen.add(event["id"])
                    rows.append((date, f"{date.strftime('%a')}: {result_name(event)}", event))
    except RuntimeError as error:
        print(f"Week ahead economic data unavailable: {error}")
        status.note("weekly:ahead", "Next week's calendar was unavailable for the weekly PDF.")

    rows.sort(key=lambda r: ("rate decision" not in r[2].get("title", "").lower(),
                             r[2].get("forecast") is None, r[0]))
    lines = [(date, text) for date, text, _ in rows[:MAX_AHEAD]]

    try:
        start, end = day_range(monday)[0], day_range(monday + timedelta(days=4))[1]
        found = scan(["description", "market_cap_basic", "earnings_release_next_date"],
                     [{"left": "earnings_release_next_date", "operation": "in_range",
                       "right": [start, end]}])
        names = [r["symbol"].partition(":")[2] for r in large_us_companies(found, 50e9)[:6]]
        earnings = ", ".join(names)
    except RuntimeError as error:
        print(f"Week ahead earnings unavailable: {error}")
        earnings = ""
    return [text for _, text in sorted(lines, key=lambda r: r[0])], earnings


def clean(text):
    """Keep characters the PDF font can show, then make the text safe for layout."""
    text = re.sub(r"[^\x20-\x7e\xa0-\xff–—’‘“”•]", "", str(text)).strip()
    return escape(text)


def sentiment_label(score):
    """Return the label and colour for a score, using five equal bands."""
    bands = [(20, "Extreme Fear", "#e5484d"), (40, "Fear", "#e88b00"), (60, "Neutral", "#f0c929"),
             (80, "Greed", "#9fd400"), (100, "Extreme Greed", "#12c47d")]
    return next((name, colour) for limit, name, colour in bands if score <= limit)


def gauge(score):
    """Draw a dark gauge card: a five-colour arc, a marker, the score and a label."""
    from math import cos, pi, sin

    from reportlab.graphics.shapes import Circle, Drawing, PolyLine, Rect, String
    from reportlab.lib import colors

    width, height, cx, cy, radius = 148, 112, 74, 34, 50
    drawing = Drawing(width, height)
    drawing.add(Rect(0, 0, width, height, rx=10, ry=10, strokeWidth=0.8,
                     fillColor=colors.HexColor(PAGE_COLOUR),
                     strokeColor=colors.HexColor("#3a3f4c")))
    drawing.add(String(12, height - 18, "Market Sentiment", fontName="Helvetica",
                       fontSize=9.5, fillColor=colors.HexColor("#9aa3b2")))

    def point(value):
        angle = pi * (1 - value / 100)
        return cx + radius * cos(angle), cy + radius * sin(angle)

    segments = ((0, "#e5484d"), (20, "#e88b00"), (40, "#f0c929"), (60, "#9fd400"), (80, "#12c47d"))
    for start, colour in segments:
        points = []
        for step in range(13):
            points += point(start + 2.5 + (15 * step / 12))
        drawing.add(PolyLine(points, strokeColor=colors.HexColor(colour), strokeWidth=6.5,
                             strokeLineCap=1, strokeLineJoin=1))

    x, y = point(score)
    drawing.add(Circle(x, y, 5.5, fillColor=colors.white,
                       strokeColor=colors.HexColor(PAGE_COLOUR), strokeWidth=2))
    drawing.add(String(cx, cy + 3, str(score), fontName="Helvetica-Bold", fontSize=21,
                       fillColor=colors.white, textAnchor="middle"))

    label, colour = sentiment_label(score)
    pill = max(38, 5.2 * len(label) + 14)
    drawing.add(Rect(cx - pill / 2, cy - 19, pill, 14, rx=7, ry=7, strokeWidth=0,
                     fillColor=colors.HexColor(colour)))
    drawing.add(String(cx, cy - 15, label, fontName="Helvetica-Bold", fontSize=8.2,
                       textAnchor="middle", fillColor=colors.white))
    return drawing


def build_pdf(day, items, notes, ahead, earnings_ahead):
    """Return the PDF as bytes, always on one page."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (KeepInFrame, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    page = colors.HexColor(PAGE_COLOUR)
    ink = colors.HexColor("#f2f4f8")
    muted = colors.HexColor("#9aa3b2")
    line = colors.HexColor("#3a3f4c")
    accent = colors.HexColor("#4ade80")
    band = colors.HexColor("#2c303c")

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=8.8, leading=11.6, textColor=ink)
    small = ParagraphStyle("small", parent=body, fontSize=7.4, leading=9.6, textColor=muted)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.2, leading=10)
    head = ParagraphStyle("head", parent=body, fontName="Helvetica-Bold", fontSize=7.4,
                          leading=9.4, textColor=muted)
    title = ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=20,
                           leading=24, textColor=ink)
    section = ParagraphStyle("section", parent=body, fontName="Helvetica-Bold", fontSize=10.5,
                             leading=13, textColor=accent, spaceBefore=9, spaceAfter=3)

    monday, friday = week_of(day)
    span = (f"{monday.strftime('%b')} {monday.day} to {friday.strftime('%b')} {friday.day}, "
            f"{friday.year}")
    flow = [Paragraph("Weekly Market Recap", title), Paragraph(clean(span), small), Spacer(1, 7)]

    regimes = [i for i in items if i["kind"] == "regime"]
    if regimes:
        last = regimes[-1]
        scored = [r for r in regimes if r.get("score") is not None]
        text = ["<b>Market sentiment</b>", clean(last["detail"])]
        if len(scored) > 1:
            text.append(f"Week's change: {scored[0]['score']} to {scored[-1]['score']}")
        text.append(
            "<font size=7 color='#9aa3b2'>Our own score from the VIX, yield curve, breadth "
            "and trend. It isn't CNN's Fear &amp; Greed Index.</font>")
        words = Paragraph("<br/>".join(text), body)
        if scored:
            row = [[gauge(scored[-1]["score"]), words]]
            strip = Table(row, colWidths=[2.2 * inch, 4.9 * inch])
        else:
            strip = Table([[words]], colWidths=[7.1 * inch])
        strip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), band),
                                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                   ("TOPPADDING", (0, 0), (-1, -1), 6),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
        flow.append(strip)

    if notes:
        flow.append(Paragraph("Key takeaways", section))
        flow += [Paragraph(f"•&nbsp; {clean(note)}", body) for note in notes]

    def table(header, rows, widths, flags=False):
        first = with_flag if flags else coloured
        data = [[Paragraph(clean(c), head) for c in header]]
        data += [[Paragraph(first(row[0]), cell), *[Paragraph(coloured(c), cell) for c in row[1:]]]
                 for row in rows]
        grid = Table(data, colWidths=[w * inch for w in widths], repeatRows=1)
        grid.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
                                  ("LINEBELOW", (0, 0), (-1, 0), 0.8, muted),
                                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                  ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        return grid

    results = top_results(items)
    flow.append(Paragraph("Economic results", section))
    if results:
        rows = [[result_name(i), value(i, "actual"), value(i, "forecast"), value(i, "previous")]
                for i in results]
        flow.append(table(["Event", "Actual", "Forecast", "Previous"], rows, [4.1, 1, 1, 1],
                          flags=True))
    else:
        flow.append(Paragraph("No high-importance results were saved this week.", body))

    earnings = top_earnings(items)
    flow.append(Paragraph("Biggest earnings reports", section))
    if earnings:
        rows = []
        for item in earnings:
            name = item.get("description") or ""
            rows.append([f"{item['symbol'].partition(':')[2]}  {name[:30]}",
                         f"{recaps.eps(item.get('earnings_per_share_fq'))} vs "
                         f"{recaps.eps(item.get('earnings_per_share_forecast_fq'))}",
                         verdict(item.get("eps_surprise_percent_fq")),
                         money(item.get("revenue_fq")),
                         verdict(item.get("revenue_surprise_percent_fq"))])
        flow.append(table(["Company", "EPS vs est", "EPS result", "Revenue", "Revenue result"],
                          rows, [2.7, 1.1, 1.1, 1, 1.2]))
    else:
        flow.append(Paragraph("No big earnings reports were saved this week.", body))

    flow.append(Paragraph("Next week", section))
    if ahead:
        flow += [Paragraph(f"•&nbsp; {with_flag(entry)}", body) for entry in ahead]
    if earnings_ahead:
        flow.append(Paragraph(f"•&nbsp; Earnings to watch: {clean(earnings_ahead)}", body))
    if not ahead and not earnings_ahead:
        flow.append(Paragraph("Next week's calendar wasn't available.", body))

    start = min((i["at"][:10] for i in items), default=None)
    coverage = ""
    if start and start > monday.isoformat():
        moment = datetime.fromisoformat(start)
        coverage = f" Coverage starts {moment.strftime('%b')} {moment.day}, when saving began."
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "Figures come from TradingView data as posted during the week. Takeaways are "
        f"AI-written and checked against those figures.{coverage} For education only, "
        "not financial advice.", small))

    output = io.BytesIO()
    margin = 0.7 * inch
    width, height = letter
    fitted = KeepInFrame(width - 2 * margin, height - 2 * margin, flow, mode="shrink")
    frame_doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=margin, rightMargin=margin,
                                  topMargin=margin, bottomMargin=margin,
                                  title="Weekly Market Recap", author="TradingView Discord alerts")

    def paint_page(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(page)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.restoreState()

    frame_doc.build([fitted], onFirstPage=paint_page)
    return output.getvalue()


def post_file(webhook, content, filename, data):
    """Upload a file to Discord, waiting if asked to slow down."""
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n{payload}\r\n".encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
        f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
        data, f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(webhook, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "tv-top-stories/1.0"})

    for attempt in range(1, POST_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=60):
                return
        except urllib.error.HTTPError as error:
            if (error.code != 429 and error.code < 500) or attempt == POST_ATTEMPTS:
                raise RuntimeError(f"Discord returned HTTP {error.code}") from error
            time.sleep(retry_delay(error, attempt))
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == POST_ATTEMPTS:
                raise RuntimeError(f"Could not reach Discord: {error}") from error
            time.sleep(2 ** attempt)


def send_window(now):
    """True from 5 PM ET Friday until the end of Saturday."""
    if now.weekday() == SEND_WEEKDAY:
        return now.hour >= SEND_HOUR
    return now.weekday() == SEND_WEEKDAY + 1


def report_friday(now):
    return week_of(now.date())[1].isoformat()


def is_due(now):
    if not send_window(now):
        return False
    state = cal.load_state()[0]
    return state.get("weekly_sent") != report_friday(now)


def caption(day):
    monday, friday = week_of(day)
    return (f"**📄 Weekly Market Recap · {monday.strftime('%b')} {monday.day} to "
            f"{friday.strftime('%b')} {friday.day}**")


def filename(day):
    return f"weekly-market-recap-{week_of(day)[1].isoformat()}.pdf"


def build(day, now, wait_for_ai):
    """Return PDF bytes, or None to wait and retry the takeaways on a later run."""
    items = week_items(day)
    lines = source_lines(top_results(items), top_earnings(items))
    notes = takeaways(lines)
    retry_later = now.weekday() == SEND_WEEKDAY and now.hour < AI_DEADLINE_HOUR
    if notes is None and lines and wait_for_ai and retry_later and ai.enabled():
        return None
    ahead, earnings_ahead = week_ahead(day)
    return build_pdf(day, items, notes or [], ahead, earnings_ahead)


def run(webhook, now):
    if not is_due(now):
        return
    day = now.date()
    data = build(day, now, wait_for_ai=True)
    if data is None:
        print("Waiting to retry the AI takeaways on the next run.")
        return
    post_file(webhook, caption(day), filename(day), data)
    state, _ = cal.load_state()
    state["weekly_sent"] = report_friday(now)
    cal.save_state(state)
    print("Sent the weekly PDF.")


def main():
    parser = argparse.ArgumentParser(description="Send the weekly PDF to Discord.")
    parser.add_argument("--check", action="store_true", help="exit 0 if a PDF is due")
    parser.add_argument("--test", action="store_true", help="send this week so far")
    parser.add_argument("--save", help="write the PDF to this file only")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_WEEKLY_WEBHOOK_URL", "").strip()
    now = datetime.now(EASTERN)
    if args.check:
        sys.exit(0 if webhook and (args.test or is_due(now)) else 1)
    try:
        if args.save:
            with open(args.save, "wb") as output:
                output.write(build(now.date(), now, wait_for_ai=False))
            print(f"Saved {args.save}")
        elif args.test:
            if not webhook:
                sys.exit("Set the DISCORD_WEEKLY_WEBHOOK_URL environment variable first.")
            post_file(webhook, caption(now.date()) + " (test)", filename(now.date()),
                      build(now.date(), now, wait_for_ai=False))
            print("Sent a test PDF.")
        elif webhook:
            run(webhook, now)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

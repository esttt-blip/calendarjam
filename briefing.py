"""Build the 'Your day' briefing section: weather + today's events + linked emails.

For each event, scan recent inbox emails for related context (e.g. a "Premium
menu" email matched to a Spirit game tonight), and render as a clickable chip.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import Optional

import zoneinfo

from calendar_api import list_events
from weather import fetch_today_forecast, _CODE_MAP

ET = zoneinfo.ZoneInfo("America/New_York")

# Source-specific keyword sets — when an event matches these patterns, we know
# what to search the inbox for. Keys are case-insensitive substrings of event titles.
_EVENT_SEARCH_TERMS: dict[str, list[str]] = {
    "spirit": ["spirit", "audi field", "washington spirit"],
    "viva": ["viva", "teamsnap"],
    "ignite": ["ignite", "teamsnap"],
    "mystics": ["mystics", "wnba"],
    "milo": ["pet grand hotel", "milo"],
    "swanson": ["swanson", "school"],
    "golf": ["golf", "tee time"],
}

# Words to ignore when extracting fallback keywords from event titles
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "at", "on", "to", "for", "with",
    "from", "by", "vs", "vs.", "&", "drive", "go", "with", "is", "are", "be",
    "all", "day", "time", "today", "tomorrow", "morning", "evening", "afternoon",
}


def decode_subject(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def event_keywords(event: dict) -> list[str]:
    """Extract search keywords for an event title. Returns lowercase list."""
    title = (event.get("summary") or "").lower()

    # First try mapped sources
    for key, terms in _EVENT_SEARCH_TERMS.items():
        if key in title:
            return terms

    # Fallback: extract capitalized words from original title
    words = re.findall(r"[A-Z][a-zA-Z']+", event.get("summary") or "")
    return [w.lower() for w in words if w.lower() not in _STOPWORDS][:3]


def gmail_message_link(message_id: str) -> str:
    """Build a gmail.com URL that opens this specific message."""
    # Strip angle brackets, URL-encode
    clean = message_id.strip("<>").replace("@", "%40")
    return f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{clean}"


def find_related_emails(
    mail: imaplib.IMAP4_SSL,
    keywords: list[str],
    since_days: int = 7,
    max_results: int = 2,
) -> list[dict]:
    """Search inbox for emails matching any of the keywords. Returns top matches."""
    if not keywords:
        return []

    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")

    matches: list[dict] = []
    seen_ids: set[str] = set()

    for keyword in keywords:
        # IMAP SUBJECT search; OR with body would be slower
        try:
            _, ids = mail.search(None, f'(SINCE "{since}" SUBJECT "{keyword}")')
        except Exception:
            continue

        for msg_id in ids[0].split()[-max_results:]:  # Most recent first
            if msg_id.decode() in seen_ids:
                continue
            seen_ids.add(msg_id.decode())

            try:
                _, data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID DATE)])")
                if not data or not data[0]:
                    continue
                raw_headers = data[0][1]
                msg = email.message_from_bytes(raw_headers)
                subject = decode_subject(str(msg.get("Subject", "")))
                sender = str(msg.get("From", "")).split("<")[0].strip().strip('"')
                msgid = str(msg.get("Message-ID", "")).strip()

                # Skip our own calendarjam emails
                if "calendarjam" in subject.lower():
                    continue

                matches.append({
                    "subject": subject,
                    "from": sender or "—",
                    "message_id": msgid,
                    "link": gmail_message_link(msgid) if msgid else "",
                })
            except Exception:
                continue

        if len(matches) >= max_results:
            break

    return matches[:max_results]


# ─────────────────────────── outdoor classification ───────────────────────────

OUTDOOR_SIGNALS = (
    "viva", "ignite", "soccer", "baseball", "practice", "game", "spirit",
    "audi field", "park", "field", "outdoor", "tournament", "golf",
)


def is_outdoor(event: dict) -> bool:
    blob = ((event.get("summary") or "") + " " + (event.get("location") or "")).lower()
    return any(s in blob for s in OUTDOOR_SIGNALS)


# ─────────────────────────── rendering ───────────────────────────


def _fmt_time(event: dict) -> str:
    s = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    if s and "T" in s:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)
        time_str = dt.strftime("%-I:%M")
        ampm = dt.strftime("%p").lower()
        return (
            f'<span style="font-family:monospace;font-weight:600;color:#1a1a2e;font-size:14px">{time_str}</span>'
            f' <span style="color:#888;font-size:11px">{ampm}</span>'
        )
    return '<span style="font-family:monospace;font-weight:600;color:#888">all day</span>'


def _weather_chip(event: dict, forecast: dict | None) -> str:
    if not forecast or not is_outdoor(event):
        return ""
    if forecast["precip_pct"] >= 40:
        return (
            f'<span style="background:#e0e8ff;color:#1f3d8a;font-size:10px;font-weight:700;'
            f'padding:2px 7px;border-radius:10px;margin-left:6px;white-space:nowrap">'
            f'☔ {forecast["precip_pct"]}% rain</span>'
        )
    if forecast["high_f"] >= 85 or forecast["low_f"] <= 35:
        return (
            f'<span style="background:#fff0e0;color:#8a3d1f;font-size:10px;font-weight:700;'
            f'padding:2px 7px;border-radius:10px;margin-left:6px;white-space:nowrap">'
            f'{forecast["emoji"]} {forecast["high_f"]}°F</span>'
        )
    return ""


def _email_chip(related: dict) -> str:
    subject = (related.get("subject") or "(no subject)")[:60]
    sender = (related.get("from") or "—")[:30]
    link = related.get("link", "")
    inner = (
        f'<span style="color:#5a5a7a;font-weight:600">📧 {sender}:</span>'
        f' <span style="color:#666">{subject}</span>'
    )
    if link:
        return (
            f'<div style="margin-top:6px"><a href="{link}" '
            f'style="display:inline-block;background:#f0f4ff;color:#1a1a2e;'
            f'text-decoration:none;font-size:12px;padding:5px 9px;border-radius:6px;'
            f'border:1px solid #d8e0f5">{inner}</a></div>'
        )
    return f'<div style="margin-top:6px;font-size:12px">{inner}</div>'


def render_event_row(event: dict, forecast: dict | None, related_emails: list[dict]) -> str:
    """Render one row in the 'Your day' table. (No per-event weather chip —
    the day's forecast is shown once in the section header.)"""
    time_html = _fmt_time(event)
    title = (event.get("summary") or "(untitled)")[:90]
    location = (event.get("location") or "")[:70]

    description = (event.get("description") or "")
    description = re.sub(r"<[^>]+>", " ", description)
    description = re.sub(r"\s+", " ", description).strip()[:140]

    loc_html = (
        f'<div style="color:#888;font-size:12px;margin-top:3px">📍 {location}</div>'
        if location else ""
    )
    desc_html = (
        f'<div style="color:#666;font-size:12px;margin-top:5px;line-height:1.4;'
        f'font-style:italic">{description}{"…" if len(description) >= 140 else ""}</div>'
        if description else ""
    )
    emails_html = "".join(_email_chip(e) for e in related_emails)

    return f"""
    <tr><td style="padding:12px 0;border-bottom:1px solid #f0f0f0">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td width="78" style="vertical-align:top;padding-top:2px">{time_html}</td>
        <td>
          <div style="color:#1a1a2e;font-weight:600;font-size:14px;line-height:1.35">{title}</div>
          {loc_html}{desc_html}{emails_html}
        </td>
      </tr></table>
    </td></tr>"""


def render_weather_strip(forecast: dict | None) -> str:
    if not forecast:
        return ""
    return f"""
    <tr><td style="padding:14px 28px 0 28px">
      <div style="background:linear-gradient(135deg,#e8f0ff 0%,#fff0e8 100%);
                  border-radius:10px;padding:14px 16px">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="60" style="font-size:32px;line-height:1">{forecast["emoji"]}</td>
          <td>
            <div style="color:#1a1a2e;font-weight:700;font-size:15px">{forecast["label"]}</div>
            <div style="color:#555;font-size:13px;margin-top:2px">
              {forecast["high_f"]}°F / {forecast["low_f"]}°F · {forecast["precip_pct"]}% rain chance
            </div>
          </td>
        </tr></table>
      </div>
    </td></tr>"""


def build_briefing(gmail_address: str, app_password: str) -> tuple[str, int]:
    """Fetch today's events + weather + related emails, return (html, event_count)."""
    now_et = datetime.now(ET)
    start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    events = list_events(
        time_min_iso=start.isoformat(),
        time_max_iso=end.isoformat(),
        max_results=50,
    )
    forecast = fetch_today_forecast()

    # Connect to Gmail once for all related-email searches
    related_by_event: list[list[dict]] = [[] for _ in events]
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, app_password)
        mail.select("inbox")
        for i, ev in enumerate(events):
            kws = event_keywords(ev)
            if kws:
                related_by_event[i] = find_related_emails(mail, kws, since_days=7, max_results=2)
        mail.logout()
    except Exception as e:
        print(f"  [briefing] inbox scan failed: {e}")

    rows = "".join(render_event_row(ev, forecast, related_by_event[i]) for i, ev in enumerate(events))
    weather_strip = render_weather_strip(forecast)
    today_label = start.strftime("%A, %B %-d")

    html = f"""
    {weather_strip}
    <tr><td style="padding:18px 28px 8px 28px">
      <span style="background:#3a3a55;color:#fff;font-size:11px;font-weight:700;
                   padding:4px 10px;border-radius:4px;letter-spacing:0.5px;
                   text-transform:uppercase">Your day</span>
      <span style="color:#666;font-size:13px;margin-left:8px">{len(events)} events on {today_label}</span>
    </td></tr>
    <tr><td style="padding:12px 28px 16px 28px">
      <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
    </td></tr>"""

    return html, len(events)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent / ".env")
    html, n = build_briefing(os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD"))
    print(f"Built briefing for {n} events. HTML length: {len(html)} chars.")

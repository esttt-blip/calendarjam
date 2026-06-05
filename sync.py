#!/usr/bin/env python3
"""calendarjam daily sync.

Architecture: runs on GitHub Actions daily at 6 AM ET. Fetches all sources,
classifies each event, writes high-confidence items directly to Google
Calendar, surfaces ambiguous Gmail items in a daily summary email. State
(synced IDs, pending review queue, last-summary message ID) is committed
back to the repo by the Action.
"""

from __future__ import annotations

import email
import hashlib
import html as html_module
import imaplib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from icalendar import Calendar

import activity
from briefing import build_briefing
from calendar_api import create_event
from classify import CalendarWrite, classify

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
PENDING_FILE = BASE_DIR / "pending_events.json"
SYNCED_FILE = BASE_DIR / "synced_events.json"
LAST_SUMMARY_FILE = BASE_DIR / "last_summary.json"

# Load .env for local dev; GitHub Actions sets env vars directly
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

EVENT_SUBJECTS = [
    "invite", "invitation", "you're invited", "save the date",
    "registration confirmed", "you're registered", "you're signed up",
    "booking confirmed", "reservation confirmed", "ticket",
    "schedule", "rescheduled", "cancelled", "postponed",
    "practice", "game", "tournament", "meet", "appointment",
    "confirmed", "confirmation", "reminder", "your visit",
    "your appointment", "your booking", "your reservation",
    "is scheduled", "has been scheduled", "upcoming",
]

_DATETIME_RE = re.compile(
    r"""
    (?:\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b.{0,40}?\b\d{1,2}(?::\d{2})?\s*[ap]m\b)
    |
    (?:\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday
           |january|february|march|april|may|june|july|august|september
           |october|november|december)
       .{0,60}?\b\d{1,2}(?::\d{2})?\s*[ap]m\b)
    |
    (?:\b\d{1,2}(?::\d{2})?\s*[ap]m\b.{0,40}?
       (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday
         |\d{1,2}/\d{1,2}))
    """,
    re.IGNORECASE | re.VERBOSE,
)


def decode_subject(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def strip_html(raw: str) -> str:
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_module.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def has_appointment_signal(text: str) -> bool:
    return bool(_DATETIME_RE.search(text))


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def stable_id(source: str, uid: str) -> str:
    return hashlib.md5(f"{source}:{uid}".encode()).hexdigest()


def normalize_dt(dt_value) -> str | None:
    if dt_value is None:
        return None
    dt = dt_value.dt
    if hasattr(dt, "hour"):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).isoformat()


# ─────────────────────────── source fetchers ───────────────────────────


def fetch_ics_feed(
    source_name: str,
    url: str,
    now: datetime,
    cutoff: datetime,
    synced_ids: set,
    filter_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    lookback_days: int = 0,
) -> list[dict]:
    events = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; calendarjam/1.0)"}
    try:
        resp = requests.get(url, timeout=30, headers=headers)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
    except Exception as e:
        print(f"  [{source_name}] fetch failed: {e}", file=sys.stderr)
        return events

    earliest = now - timedelta(days=lookback_days)

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID", ""))
        eid = stable_id(source_name, uid)
        if eid in synced_ids:
            continue

        summary = str(component.get("SUMMARY", "Untitled"))
        summary_lower = summary.lower()

        if filter_keywords and not any(kw.lower() in summary_lower for kw in filter_keywords):
            continue
        if exclude_keywords and any(kw.lower() in summary_lower for kw in exclude_keywords):
            continue

        start_str = normalize_dt(component.get("DTSTART"))
        if start_str is None:
            continue

        start_dt = datetime.fromisoformat(start_str)
        if start_dt < earliest or start_dt > cutoff:
            continue

        events.append({
            "id": eid,
            "source": source_name,
            "title": summary,
            "start": start_str,
            "end": normalize_dt(component.get("DTEND")),
            "location": str(component.get("LOCATION", "") or ""),
            "description": str(component.get("DESCRIPTION", "") or "")[:500],
            "uid": uid,
        })

    return events


def parse_ics_bytes(data: bytes, source: str, msg_id: str, now: datetime, cutoff: datetime, synced_ids: set) -> list[dict]:
    events = []
    try:
        cal = Calendar.from_ical(data)
    except Exception:
        return events

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID", msg_id))
        eid = stable_id(source, uid)
        if eid in synced_ids:
            continue
        start_str = normalize_dt(component.get("DTSTART"))
        if not start_str:
            continue
        start_dt = datetime.fromisoformat(start_str)
        if start_dt < now or start_dt > cutoff:
            continue
        events.append({
            "id": eid,
            "source": source,
            "title": str(component.get("SUMMARY", "Untitled")),
            "start": start_str,
            "end": normalize_dt(component.get("DTEND")),
            "location": str(component.get("LOCATION", "") or ""),
            "description": str(component.get("DESCRIPTION", "") or "")[:500],
            "uid": uid,
        })
    return events


def fetch_gmail_events(now: datetime, cutoff: datetime, synced_ids: set) -> list[dict]:
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        print("  [Gmail] skipped — credentials not set", file=sys.stderr)
        return []

    events = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, app_password)
        mail.select("inbox")

        since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        _, msg_ids = mail.search(None, f'(SINCE "{since}")')

        for msg_id in msg_ids[0].split():
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw = data[0][1]
            msg = email.message_from_bytes(raw)

            subject = decode_subject(str(msg.get("Subject", "")))
            subject_lower = subject.lower()

            # Skip our own emails (feedback loop prevention) — anything we sent
            # has "calendarjam" in the subject, including replies/acks/previews.
            if "calendarjam" in subject_lower:
                continue

            is_event_like = any(kw in subject_lower for kw in EVENT_SUBJECTS)
            msg_has_ics = False
            plain_body: str | None = None
            html_body: str | None = None

            for part in msg.walk():
                mime = part.get_content_type()
                filename = part.get_filename() or ""

                if mime == "text/calendar" or filename.lower().endswith(".ics"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        parsed = parse_ics_bytes(
                            payload, "Gmail invite",
                            msg_id.decode(), now, cutoff, synced_ids,
                        )
                        events.extend(parsed)
                        msg_has_ics = True
                elif mime == "text/plain" and plain_body is None:
                    plain_body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
                elif mime == "text/html" and html_body is None:
                    raw_html = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
                    html_body = strip_html(raw_html)

            if not msg_has_ics:
                body = (plain_body or html_body or "").strip()
                should_surface = is_event_like or has_appointment_signal(body)
                if should_surface and body:
                    eid = stable_id("gmail-email", msg_id.decode())
                    if eid not in synced_ids:
                        events.append({
                            "id": eid,
                            "source": "Gmail",
                            "title": subject or "Untitled email",
                            "start": None,
                            "end": None,
                            "location": "",
                            "description": body[:1500],
                            "uid": msg_id.decode(),
                            "needs_review": True,
                            "from": str(msg.get("From", "")),
                        })

        mail.logout()
    except Exception as e:
        print(f"  [Gmail] error: {e}", file=sys.stderr)

    return events


# ─────────────────────────── calendar writes ───────────────────────────


def write_to_calendar(w: CalendarWrite) -> dict | None:
    """Create the calendar event. Return the resource (or None on failure)."""
    try:
        result = create_event(
            summary=w.summary,
            start_iso=w.start_iso,
            end_iso=w.end_iso,
            location=w.location,
            description=w.description,
            attendees=w.attendees or None,
            transparency=w.transparency,
        )
        print(f"  ✓ [{w.source_name}] {w.summary}  ({w.start_iso[:16]})")
        activity.append("added", w.summary, w.source_name, when=w.start_iso[:16])
        return result
    except Exception as e:
        print(f"  ✗ [{w.source_name}] {w.summary} — failed: {e}", file=sys.stderr)
        return None


# ─────────────────────────── summary email ───────────────────────────


def _fmt_when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %b %-d  ·  %-I:%M %p")
    except Exception:
        return iso[:10]


def _source_chip(source: str) -> str:
    palette = {
        "VIVA ⚽": "#2ecc71",
        "Ignite ⚾": "#e74c3c",
        "Swanson 📚": "#3498db",
        "Gmail": "#f39c12",
        "Gmail invite": "#f39c12",
        "Karma Yoga 🧘": "#9b59b6",
    }
    color = palette.get(source, "#888")
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-size:10px;padding:2px 7px;border-radius:10px;font-weight:600;'
        f'letter-spacing:0.3px;text-transform:uppercase">{source}</span>'
    )


def format_added_row(w: CalendarWrite) -> str:
    """A row in the 'Added (no action)' section."""
    return f"""
<tr>
  <td style="padding:8px 0;border-bottom:1px solid #f0f0f0">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="24" style="vertical-align:top;padding-top:2px">
        <span style="color:#2ecc71;font-size:16px;font-weight:700">✓</span>
      </td>
      <td>
        {_source_chip(w.source_name)}
        <div style="color:#1a1a2e;font-weight:600;font-size:14px;margin-top:4px">{w.summary}</div>
        <div style="color:#888;font-size:12px;margin-top:2px">{_fmt_when(w.start_iso)}</div>
      </td>
    </tr></table>
  </td>
</tr>"""


def format_review_item(idx: int, item: dict) -> str:
    """A row in the 'Needs decision' section — numbered for reply matching."""
    title = item.get("title", "Untitled")
    sender = item.get("from", "").split("<")[0].strip() or "—"
    desc = (item.get("description") or "")[:160].replace("\n", " ").replace("\r", " ").strip()

    return f"""
<tr>
  <td style="padding:14px 0;border-bottom:1px solid #eee">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="36" style="vertical-align:top;padding-top:2px">
        <span style="display:inline-block;background:#ff6b35;color:#fff;
                     border-radius:50%;width:28px;height:28px;line-height:28px;
                     text-align:center;font-weight:700;font-size:14px">{idx}</span>
      </td>
      <td>
        <div style="color:#1a1a2e;font-weight:600;font-size:15px;line-height:1.3">{title}</div>
        <div style="color:#888;font-size:12px;margin-top:2px">from {sender}</div>
        <div style="color:#555;font-size:13px;margin-top:6px;line-height:1.45">{desc}…</div>
        <div style="margin-top:10px">
          <span style="background:#f0f0f0;color:#666;padding:3px 8px;border-radius:4px;
                       font-family:monospace;font-size:12px">reply: yes {idx}</span>
          <span style="margin-left:6px;background:#f0f0f0;color:#666;padding:3px 8px;
                       border-radius:4px;font-family:monospace;font-size:12px">no {idx}</span>
        </div>
      </td>
    </tr></table>
  </td>
</tr>"""


def _format_activity_section() -> str:
    """Render last-5-days activity grouped by day."""
    entries = activity.recent(days=5)
    if not entries:
        return ""

    # Group by date (local ET)
    from collections import defaultdict
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        try:
            dt = datetime.fromisoformat(e["ts"])
            day = dt.astimezone().strftime("%a %b %-d")
        except Exception:
            day = "—"
        by_day[day].append(e)

    action_icons = {
        "added": ("✓", "#2ecc71"),
        "dismissed": ("✗", "#888"),
        "acked": ("✓", "#2ecc71"),
        "skipped": ("✗", "#888"),
    }

    day_html = []
    for day, items in by_day.items():
        rows = []
        for it in items:
            icon, color = action_icons.get(it["action"], ("·", "#888"))
            title = it.get("title", "")[:70]
            source = it.get("source", "")
            source_str = f' <span style="color:#aaa;font-size:11px">· {source}</span>' if source else ""
            rows.append(
                f'<div style="padding:3px 0;font-size:13px;color:#555">'
                f'<span style="color:{color};font-weight:700;margin-right:6px">{icon}</span>'
                f'{title}{source_str}'
                f'</div>'
            )
        day_html.append(
            f'<div style="margin-bottom:10px">'
            f'<div style="color:#888;font-size:11px;font-weight:700;letter-spacing:0.5px;'
            f'text-transform:uppercase;margin-bottom:4px">{day}</div>'
            f'{"".join(rows)}'
            f'</div>'
        )

    return f"""
    <tr><td style="padding:20px 28px 8px 28px">
      <span style="background:#f5f5f5;color:#666;font-size:11px;font-weight:700;
                   padding:4px 10px;border-radius:4px;letter-spacing:0.5px;
                   text-transform:uppercase">Recent activity</span>
    </td></tr>
    <tr><td style="padding:12px 28px 16px 28px">
      {"".join(day_html)}
    </td></tr>"""


def send_summary_email(
    added: list[CalendarWrite],
    pending: list[dict],
) -> str | None:
    """Send the daily summary. Returns the Message-ID for reply matching.

    Always sends an email — even on quiet days — so the user has a daily
    heartbeat confirming the sync ran. Email is now intentionally minimal:
    counts + 'Open app' button + today's briefing + recent activity. Actual
    item-by-item review happens in the Streamlit app.
    """
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    app_url = os.getenv("APP_URL", "https://share.streamlit.io")
    if not gmail_address or not app_password:
        print("  [Email] skipped — credentials not set")
        return None

    today = datetime.now().strftime("%A, %B %-d")
    n_added = len(added)
    n_pending = len(pending)

    # Subject is now status-driven and stays short
    if n_pending:
        subject = f"📅 calendarjam — {n_pending} pending"
    elif n_added:
        subject = f"📅 calendarjam — {n_added} added today"
    else:
        subject = "📅 calendarjam — all clear"

    # ── Status banner: pending/added counts in a glanceable header ──
    if n_pending and n_added:
        status_label = f"{n_added} added · {n_pending} need your review"
        status_color = "#ff6b35"
    elif n_pending:
        status_label = f"{n_pending} item{'s' if n_pending != 1 else ''} need your review"
        status_color = "#ff6b35"
    elif n_added:
        status_label = f"{n_added} added to your calendar"
        status_color = "#2ecc71"
    else:
        status_label = "All clear · nothing new today"
        status_color = "#2ecc71"

    status_section = f"""
    <tr><td style="padding:22px 28px 8px 28px;text-align:center">
      <div style="display:inline-block;background:{status_color}15;color:{status_color};
                  padding:6px 14px;border-radius:20px;font-size:13px;font-weight:700">
        ✓ Sync ran · {status_label}
      </div>
    </td></tr>"""

    # ── Open app button: primary action, big and obvious ──
    cta_section = ""
    if n_pending:
        cta_section = f"""
        <tr><td style="padding:12px 28px 4px 28px;text-align:center">
          <a href="{app_url}" style="display:inline-block;background:#1a1a2e;color:#fff;
                                      text-decoration:none;padding:14px 32px;border-radius:8px;
                                      font-size:15px;font-weight:700;letter-spacing:0.3px">
            Open calendarjam &rarr;
          </a>
          <div style="color:#888;font-size:12px;margin-top:8px">
            Tap to review your {n_pending} pending item{'s' if n_pending != 1 else ''}
          </div>
        </td></tr>"""

    # ── Today's briefing (weather + calendar + linked emails) ──
    briefing_section = ""
    try:
        briefing_html, briefing_count = build_briefing(gmail_address, app_password)
        if briefing_count:
            briefing_section = briefing_html
    except Exception as e:
        print(f"  [briefing] failed: {e}", file=sys.stderr)

    # ── Recent activity: last 5 days of decisions/adds for context ──
    activity_section = _format_activity_section()

    html_body = f"""<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:24px 12px">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="max-width:580px;background:#fff;border-radius:12px;
                    overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
        <tr><td style="background:#1a1a2e;padding:18px 28px">
          <p style="margin:0;color:#fff;font-size:20px;font-weight:700">📅 calendarjam</p>
          <p style="margin:3px 0 0 0;color:#aaa;font-size:12px">{today}</p>
        </td></tr>
        {status_section}
        {cta_section}
        {briefing_section}
        {activity_section}
        <tr><td style="padding:14px 28px;background:#fafafa;color:#999;
                       font-size:11px;text-align:center">
          Daily sync runs at 6:15 AM ET · <a href="{app_url}" style="color:#999">Review in app</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"calendarjam <{gmail_address}>"
    msg["To"] = gmail_address
    # Stable Message-ID for reply matching
    msg_id = f"<calendarjam-{datetime.now().strftime('%Y%m%d%H%M%S')}@calendarjam.local>"
    msg["Message-ID"] = msg_id
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.send_message(msg)
        print(f"  ✓ [Email] sent — {n_added} added, {n_pending} pending")
        return msg_id
    except Exception as e:
        print(f"  ✗ [Email] failed: {e}", file=sys.stderr)
        return None


# ─────────────────────────── main ───────────────────────────


def main():
    config = load_json(CONFIG_FILE, {})
    synced_ids: set = set(load_json(SYNCED_FILE, []))
    pending: list = load_json(PENDING_FILE, [])

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=config.get("look_ahead_days", 60))

    all_new: list[dict] = []

    for feed in config.get("ics_feeds", []):
        if not feed.get("enabled", True):
            continue
        url = feed.get("url", "")
        if url.startswith("PASTE_"):
            continue
        print(f"Fetching {feed['name']}...")
        fetched = fetch_ics_feed(
            feed["name"], url, now, cutoff, synced_ids,
            filter_keywords=feed.get("filter_keywords"),
            exclude_keywords=feed.get("exclude_keywords"),
            lookback_days=feed.get("lookback_days", 0),
        )
        print(f"  {len(fetched)} new")
        all_new.extend(fetched)

    print("Scanning Gmail...")
    gmail_events = fetch_gmail_events(now, cutoff, synced_ids)
    print(f"  {len(gmail_events)} new")
    all_new.extend(gmail_events)

    # Classify each event
    auto_writes: list[CalendarWrite] = []
    new_pending: list[dict] = []
    skipped_ids: list[str] = []
    existing_pending_ids = {p["id"] for p in pending}

    for event in all_new:
        decision, write = classify(event)
        if decision == "auto" and write:
            auto_writes.append(write)
        elif decision == "review" and event["id"] not in existing_pending_ids:
            new_pending.append(event)
        elif decision == "skip":
            skipped_ids.append(event["id"])

    if skipped_ids:
        print(f"\nSkipped {len(skipped_ids)} response notifications")
        synced_ids.update(skipped_ids)

    # Write auto items to calendar
    print(f"\nWriting {len(auto_writes)} events to calendar...")
    successful_writes: list[CalendarWrite] = []
    for w in auto_writes:
        if write_to_calendar(w):
            successful_writes.append(w)
            synced_ids.add(w.source_id)

    # Merge new review items into pending queue
    pending.extend(new_pending)

    # Send summary email and remember Message-ID for reply matching
    msg_id = send_summary_email(successful_writes, pending)

    # Persist state
    save_json(SYNCED_FILE, sorted(synced_ids))
    save_json(PENDING_FILE, pending)

    if msg_id and pending:
        save_json(LAST_SUMMARY_FILE, {
            "message_id": msg_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "pending_ids": [p["id"] for p in pending],
        })

    print(f"\nDone. {len(successful_writes)} added to calendar, {len(pending)} awaiting review.")


if __name__ == "__main__":
    main()

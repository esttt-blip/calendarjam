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

            # Skip our own summary emails (feedback loop prevention)
            if "calendarjam" in subject_lower and ("ready to review" in subject_lower or "added" in subject_lower):
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
        return result
    except Exception as e:
        print(f"  ✗ [{w.source_name}] {w.summary} — failed: {e}", file=sys.stderr)
        return None


# ─────────────────────────── summary email ───────────────────────────


def format_added_row(w: CalendarWrite) -> str:
    try:
        dt = datetime.fromisoformat(w.start_iso.replace("Z", "+00:00"))
        when = dt.strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        when = w.start_iso[:10]
    return (
        f'<li style="margin:4px 0;color:#444;font-size:14px">'
        f'<span style="color:#1a1a2e;font-weight:600">{w.summary}</span>'
        f' &mdash; <span style="color:#666">{when}</span>'
        f'</li>'
    )


def format_review_item(idx: int, item: dict) -> str:
    title = item.get("title", "Untitled")
    sender = item.get("from", "").split("<")[0].strip() or "—"
    desc = (item.get("description") or "")[:200]
    return f"""
<tr>
  <td style="padding:14px 0;border-bottom:1px solid #eee;vertical-align:top">
    <div style="display:flex;align-items:flex-start">
      <div style="background:#1a1a2e;color:#fff;border-radius:50%;width:24px;height:24px;
                  display:inline-block;text-align:center;line-height:24px;font-weight:700;
                  font-size:13px;margin-right:12px;flex-shrink:0">{idx}</div>
      <div style="flex:1">
        <div style="color:#1a1a2e;font-weight:600;font-size:15px">{title}</div>
        <div style="color:#888;font-size:12px;margin-top:2px">from {sender}</div>
        <div style="color:#555;font-size:13px;margin-top:6px;line-height:1.45">{desc}…</div>
      </div>
    </div>
  </td>
</tr>"""


def send_summary_email(
    added: list[CalendarWrite],
    pending: list[dict],
) -> str | None:
    """Send the daily summary. Returns the Message-ID for reply matching."""
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        print("  [Email] skipped — credentials not set")
        return None
    if not added and not pending:
        print("  [Email] nothing new — skipping")
        return None

    today = datetime.now().strftime("%A, %B %-d")
    n_added = len(added)
    n_pending = len(pending)
    subject_parts = []
    if n_added:
        subject_parts.append(f"{n_added} added")
    if n_pending:
        subject_parts.append(f"{n_pending} to review")
    subject = f"📅 calendarjam — {', '.join(subject_parts)}"

    added_html = ""
    if added:
        added_html = f"""
        <tr><td style="padding:16px 28px 4px 28px">
          <div style="color:#666;font-size:12px;text-transform:uppercase;letter-spacing:1px;
                      font-weight:600;margin-bottom:8px">Added to calendar</div>
          <ul style="margin:0;padding:0 0 0 18px">
            {"".join(format_added_row(w) for w in added[:20])}
            {"<li style='color:#888;font-size:13px'>…and " + str(len(added) - 20) + " more</li>" if len(added) > 20 else ""}
          </ul>
        </td></tr>"""

    pending_html = ""
    instructions_html = ""
    if pending:
        rows = "".join(format_review_item(i + 1, p) for i, p in enumerate(pending))
        pending_html = f"""
        <tr><td style="padding:24px 28px 4px 28px">
          <div style="color:#666;font-size:12px;text-transform:uppercase;letter-spacing:1px;
                      font-weight:600;margin-bottom:8px">Need your call</div>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </td></tr>"""
        instructions_html = """
        <tr><td style="padding:16px 28px 24px 28px">
          <div style="background:#f5f5f5;border-radius:8px;padding:14px 16px;
                      color:#444;font-size:13px;line-height:1.5">
            <strong style="color:#1a1a2e">Reply to this email</strong> with:<br>
            <code style="background:#fff;padding:2px 6px;border-radius:4px;color:#1a1a2e">yes 1, 3</code>
            to add those items, or
            <code style="background:#fff;padding:2px 6px;border-radius:4px;color:#1a1a2e">no 2</code>
            to dismiss. Use <code style="background:#fff;padding:2px 6px;border-radius:4px;color:#1a1a2e">yes all</code>
            to add everything.
          </div>
        </td></tr>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 16px">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="max-width:560px;background:#fff;border-radius:12px;
                    overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">
        <tr><td style="background:#1a1a2e;padding:20px 28px">
          <p style="margin:0;color:#fff;font-size:22px;font-weight:700">📅 calendarjam</p>
          <p style="margin:4px 0 0 0;color:#aaa;font-size:13px">{today}</p>
        </td></tr>
        {added_html}
        {pending_html}
        {instructions_html}
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

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
import deliveries as deliveries_mod
import lookahead
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


# Targeted keyword set for the All Mail safety-net search. Deliberately
# narrow + appointment-specific (not "reminder/confirmed/visit" which flood
# from newsletters). Excludes promotions/social buckets and sent mail.
# Single words only (Gmail {} = OR); no embedded quotes (breaks IMAP).
_ALLMAIL_KEYWORDS = (
    "appointment reschedule rescheduled consultation evaluation "
    "booking reservation"
)
_ALLMAIL_QUERY = f'"newer_than:7d {{{_ALLMAIL_KEYWORDS}}} -category:promotions -category:social -in:sent"'


def _process_message(msg, seq: str, now, cutoff, synced_ids, seen_msgids, events, deliveries) -> None:
    """Parse one email message and append surfaced events / detected deliveries.
    Dedups by Message-ID."""
    gmid = (str(msg.get("Message-ID", "")) or seq).strip()
    if gmid in seen_msgids:
        return
    seen_msgids.add(gmid)

    subject = decode_subject(str(msg.get("Subject", "")))
    subject_lower = subject.lower()
    from_addr = str(msg.get("From", ""))

    # Skip our own emails (feedback loop prevention)
    if "calendarjam" in subject_lower:
        return

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
                events.extend(parse_ics_bytes(payload, "Gmail invite", gmid, now, cutoff, synced_ids))
                msg_has_ics = True
        elif mime == "text/plain" and plain_body is None:
            plain_body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
        elif mime == "text/html" and html_body is None:
            raw_html = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
            html_body = strip_html(raw_html)

    if msg_has_ics:
        return

    body = (plain_body or html_body or "").strip()

    # Delivery detection — MightyMeals / Walmart → auto-add to calendar,
    # don't surface for review.
    deliv = deliveries_mod.detect(from_addr, subject, body)
    if deliv:
        did = stable_id("delivery", deliv["dedup"])
        if did not in synced_ids:
            deliv["_id"] = did
            deliveries.append(deliv)
        return

    if not (is_event_like or has_appointment_signal(body)) or not body:
        return

    eid = stable_id("gmail-email", gmid)
    if eid in synced_ids:
        return
    events.append({
        "id": eid,
        "source": "Gmail",
        "title": subject or "Untitled email",
        "start": None, "end": None, "location": "",
        "description": body[:1500],
        "uid": gmid,
        "needs_review": True,
        "from": str(msg.get("From", "")),
    })


# Walmart/MightyMeals sometimes land in Promotions, so the delivery pass uses
# its own query that does NOT exclude promotions.
_DELIVERY_QUERY = '"newer_than:10d (from:walmart OR from:mightymeals) (delivery OR arrives OR order)"'


def fetch_gmail_events(now: datetime, cutoff: datetime, synced_ids: set) -> tuple[list[dict], list[dict]]:
    """Return (review_events, deliveries)."""
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        print("  [Gmail] skipped — credentials not set", file=sys.stderr)
        return [], []

    events: list[dict] = []
    deliveries: list[dict] = []
    seen_msgids: set[str] = set()
    since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, app_password)

        # Pass 1 — full inbox scan (keyword + body date/time signal).
        mail.select("inbox")
        _, ids = mail.search(None, f'(SINCE "{since}")')
        inbox_ids = ids[0].split() if ids and ids[0] else []
        for seq in inbox_ids:
            _, data = mail.fetch(seq, "(RFC822)")
            if data and data[0]:
                _process_message(email.message_from_bytes(data[0][1]), seq.decode(),
                                 now, cutoff, synced_ids, seen_msgids, events, deliveries)

        # Pass 2 — All Mail, keyword-filtered, for archived appointment emails.
        try:
            mail.select('"[Gmail]/All Mail"', readonly=True)
            _, ids2 = mail.search(None, "X-GM-RAW", _ALLMAIL_QUERY)
            allmail_ids = ids2[0].split() if ids2 and ids2[0] else []
            for seq in allmail_ids[-400:]:  # cap to bound runtime
                _, data = mail.fetch(seq, "(RFC822)")
                if data and data[0]:
                    _process_message(email.message_from_bytes(data[0][1]), seq.decode(),
                                     now, cutoff, synced_ids, seen_msgids, events, deliveries)
        except Exception as e:
            print(f"  [Gmail] All Mail scan skipped: {e}", file=sys.stderr)

        # Pass 3 — delivery senders (Walmart / MightyMeals), incl. promotions.
        try:
            mail.select('"[Gmail]/All Mail"', readonly=True)
            _, ids3 = mail.search(None, "X-GM-RAW", _DELIVERY_QUERY)
            deliv_ids = ids3[0].split() if ids3 and ids3[0] else []
            for seq in deliv_ids[-60:]:
                _, data = mail.fetch(seq, "(RFC822)")
                if data and data[0]:
                    _process_message(email.message_from_bytes(data[0][1]), seq.decode(),
                                     now, cutoff, synced_ids, seen_msgids, events, deliveries)
        except Exception as e:
            print(f"  [Gmail] delivery scan skipped: {e}", file=sys.stderr)

        mail.logout()
    except Exception as e:
        print(f"  [Gmail] error: {e}", file=sys.stderr)

    return events, deliveries


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


def _format_conflicts_section(app_url: str) -> str:
    """Conflicts + tight turnarounds — the attention items, shown near the top
    with the decisions. Includes a link to resolve them in the app."""
    try:
        scan = lookahead.scan(days=7)
    except Exception as e:
        print(f"  [lookahead] scan failed: {e}", file=sys.stderr)
        return ""

    collisions = scan.get("collisions", [])
    tight = scan.get("tight", [])
    if not collisions and not tight:
        return ""

    rows = ""
    for c in collisions:
        a_t = c["a_start"].strftime("%-I:%M%p").lower().lstrip("0")
        b_t = c["b_start"].strftime("%-I:%M%p").lower().lstrip("0")
        rows += (
            f'<div style="padding:5px 0;font-size:13px;color:#444;line-height:1.45">'
            f'<span style="color:#c0392b;font-weight:700">⚠️ {c["day"]}</span> — '
            f'<b>{c["a"]}</b> ({a_t}) overlaps <b>{c["b"]}</b> ({b_t})</div>'
        )
    for t in tight:
        rows += (
            f'<div style="padding:5px 0;font-size:13px;color:#444;line-height:1.45">'
            f'<span style="color:#d68910;font-weight:700">⏱ {t["day"]}</span> — '
            f'only {t["gap_min"]} min between <b>{t["a"]}</b> and <b>{t["b"]}</b></div>'
        )

    return f"""
    <tr><td style="padding:18px 28px 4px 28px">
      <span style="background:#fdecea;color:#c0392b;font-size:11px;font-weight:700;
                   padding:4px 10px;border-radius:4px;letter-spacing:0.5px;
                   text-transform:uppercase">⚠️ Conflicts</span>
    </td></tr>
    <tr><td style="padding:10px 28px 4px 28px">
      <div style="background:#fff6f4;border-radius:10px;padding:14px 16px">{rows}
        <div style="margin-top:10px">
          <a href="{app_url}" style="color:#c0392b;font-size:12px;font-weight:600;
             text-decoration:none">Resolve in calendarjam &rarr;</a>
        </div>
      </div>
    </td></tr>"""


def _format_horizon_section() -> str:
    """Coming up — birthdays + major holidays, shown after the week."""
    try:
        horizon = lookahead.find_horizon(21)
    except Exception:
        return ""
    birthdays = horizon.get("birthdays", [])
    holidays = horizon.get("holidays", [])
    if not birthdays and not holidays:
        return ""

    rows = ""
    for b in birthdays:
        when = "today" if b["days_out"] == 0 else f"in {b['days_out']} days"
        rows += (f'<div style="padding:4px 0;font-size:13px;color:#444">'
                 f'🎂 <b>{b["title"]}</b> — {b["day"]} <span style="color:#999">({when})</span></div>')
    for h in holidays:
        rows += f'<div style="padding:4px 0;font-size:13px;color:#444">🎉 {h["title"]} — {h["day"]}</div>'

    return f"""
    <tr><td style="padding:18px 28px 4px 28px">
      <span style="background:#f5f5f5;color:#666;font-size:11px;font-weight:700;
                   padding:4px 10px;border-radius:4px;letter-spacing:0.5px;
                   text-transform:uppercase">🎂 Coming up</span>
    </td></tr>
    <tr><td style="padding:10px 28px 8px 28px">{rows}</td></tr>"""


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

    # ── Conflicts (attention, grouped with decisions at the top) ──
    conflicts_section = _format_conflicts_section(app_url)

    # ── Today's briefing — the day ahead (weather once + events + linked) ──
    briefing_section = ""
    try:
        briefing_html, briefing_count = build_briefing(gmail_address, app_password)
        if briefing_count:
            briefing_section = briefing_html
    except Exception as e:
        print(f"  [briefing] failed: {e}", file=sys.stderr)

    # ── Coming up — birthdays + holidays (after the day) ──
    horizon_section = _format_horizon_section()

    # Order: status → decisions(CTA) + conflicts → today → coming up.
    # Recent activity intentionally lives only in the app, not the email.
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
          <a href="{app_url}" style="text-decoration:none">
            <span style="color:#fff;font-size:20px;font-weight:700">📅 calendarjam</span>
          </a>
          <p style="margin:3px 0 0 0;color:#aaa;font-size:12px">{today}</p>
        </td></tr>
        {status_section}
        {cta_section}
        {conflicts_section}
        {briefing_section}
        {horizon_section}
        <tr><td style="padding:14px 28px;background:#fafafa;color:#999;
                       font-size:11px;text-align:center">
          Daily sync runs at 6:15 AM ET · <a href="{app_url}" style="color:#999">Open calendarjam</a>
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
    gmail_events, gmail_deliveries = fetch_gmail_events(now, cutoff, synced_ids)
    print(f"  {len(gmail_events)} new, {len(gmail_deliveries)} delivery(ies)")
    all_new.extend(gmail_events)

    # Auto-add detected deliveries (MightyMeals all-day, Walmart timed window).
    # Only future-or-today deliveries — past ones are just clutter.
    import zoneinfo as _zi
    today_et = datetime.now(_zi.ZoneInfo("America/New_York")).date()
    for d in gmail_deliveries:
        try:
            if datetime.fromisoformat(d["start"][:10]).date() < today_et:
                synced_ids.add(d["_id"])  # mark seen so we don't re-evaluate
                continue
            create_event(
                summary=d["title"],
                start_iso=d["start"],
                end_iso=d["end"],
                description=d.get("description", ""),
                transparency="transparent",
                all_day=(d["kind"] == "allday"),
            )
            synced_ids.add(d["_id"])
            activity.append("added", d["title"], "Delivery", when=d["start"])
            print(f"  ✓ [Delivery] {d['title']} ({d['start']})")
        except Exception as e:
            print(f"  ✗ [Delivery] {d['title']} failed: {e}", file=sys.stderr)

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

    # Build the command-station snapshot for the web app
    print("Building dashboard snapshot...")
    try:
        import dashboard
        snap = dashboard.build_snapshot()
        print(f"  ✓ dashboard: {snap['counts']}")
    except Exception as e:
        print(f"  ✗ dashboard build failed: {e}", file=sys.stderr)

    print(f"\nDone. {len(successful_writes)} added to calendar, {len(pending)} awaiting review.")


if __name__ == "__main__":
    main()

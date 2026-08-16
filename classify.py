"""Event classification — decides what to auto-add vs surface for review.

The core rule: structured sources (ICS feeds, .ics email attachments) are
high-confidence and get written directly to the calendar. Email body matches
are lower confidence and go into the daily summary for a human yes/no.

This module also applies source-specific transforms — e.g. Swanson school
events get title reformatting, work calendar attendee, free transparency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

WORK_EMAIL = "es.scott@draftkings.com"


@dataclass
class CalendarWrite:
    """A fully-specified event ready to POST to the calendar."""

    summary: str
    start_iso: str
    end_iso: str
    location: str = ""
    description: str = ""
    attendees: list[dict] = field(default_factory=list)
    transparency: Literal["opaque", "transparent"] = "opaque"
    source_id: str = ""  # for dedup tracking
    source_name: str = ""


def _is_weekday_daytime(start_iso: str) -> bool:
    """True if event starts Mon-Fri 08:00-17:00 local-ish."""
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        # Convert to ET roughly (UTC-4 in summer / UTC-5 in winter; close enough)
        et_offset = timedelta(hours=-4 if 3 <= dt.month <= 11 else -5)
        local = dt + et_offset
        return local.weekday() < 5 and 8 <= local.hour < 17
    except Exception:
        return False


def _swanson_title(raw: str) -> str:
    """Reformat Swanson ICS names per project rules."""
    lower = raw.lower()
    if "early release" in lower and "last day" in lower:
        return "📚 Last Day of School — Early Release"
    if "early release" in lower:
        return "📚 Early Release"
    # Strip "Holiday: " prefix if present
    name = raw
    for prefix in ("holiday:", "holiday -", "school holiday:"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):].strip()
    return f"📚 School Holiday ({name})"


def transform_swanson(event: dict) -> CalendarWrite:
    """Apply Swanson school event rules.

    - Title reformat
    - Force 9 AM – 5 PM on event date
    - Transparency: free
    - Always add work email as attendee
    """
    start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
    day = start.date()
    # 9 AM ET as ISO with naive datetime — calendar_api adds timezone
    nine = f"{day.isoformat()}T09:00:00"
    five = f"{day.isoformat()}T17:00:00"

    return CalendarWrite(
        summary=_swanson_title(event["title"]),
        start_iso=nine,
        end_iso=five,
        transparency="transparent",
        attendees=[{"email": WORK_EMAIL, "responseStatus": "needsAction"}],
        source_id=event["id"],
        source_name=event["source"],
        description="Auto-added from Swanson Middle School calendar.",
    )


def transform_ics_generic(event: dict) -> CalendarWrite:
    """Generic ICS-to-calendar mapping for VIVA, Karma Yoga, Gmail invites."""
    start = event["start"]
    end = event.get("end") or _add_hour(start)

    description = event.get("description", "")
    if "VIVA" in event["source"] and not event.get("location"):
        description = (
            "⚠️ Location not in feed — verify field/venue before departure.\n\n"
            + description
        )

    write = CalendarWrite(
        summary=event["title"],
        start_iso=start,
        end_iso=end,
        location=event.get("location", ""),
        description=description.strip(),
        source_id=event["id"],
        source_name=event["source"],
    )

    if _is_weekday_daytime(start):
        write.attendees = [{"email": WORK_EMAIL, "responseStatus": "needsAction"}]

    return write


def _add_hour(start_iso: str) -> str:
    dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    return (dt + timedelta(hours=1)).isoformat()


_RESPONSE_PREFIXES = ("accepted:", "declined:", "tentative:", "canceled:", "cancelled:")


def classify(event: dict) -> tuple[Literal["auto", "review", "skip"], CalendarWrite | None]:
    """Return (decision, calendar_write_or_None).

    - auto: event will be created directly. CalendarWrite is the payload.
    - review: surface in summary email for human yes/no.
    - skip: discard entirely (mark synced, don't add or surface).
    """
    source = event.get("source", "")
    title_lower = (event.get("title") or "").lower()

    # Gmail event-like emails (body text matched) need human review
    if event.get("needs_review"):
        return ("review", None)

    # Skip iCalendar response notifications — these are notifications about
    # invites already handled in another calendar, adding them creates dupes.
    if source == "Gmail invite" and title_lower.startswith(_RESPONSE_PREFIXES):
        return ("skip", None)

    # ICS-based sources are structured enough to auto-add
    if source == "Swanson 📚":
        return ("auto", transform_swanson(event))

    if source in ("VIVA ⚽", "Gmail invite", "Karma Yoga 🧘"):
        return ("auto", transform_ics_generic(event))

    # Default: surface for review
    return ("review", None)

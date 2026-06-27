"""Google Calendar API wrapper for calendarjam.

Wraps the bits of the Calendar API we actually use: list events, create event,
update event, delete event. Uses our auth module for credentials.
"""

from __future__ import annotations

from typing import Optional

from googleapiclient.discovery import build

from auth import get_credentials


def _service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def list_events(
    calendar_id: str = "primary",
    time_min_iso: Optional[str] = None,
    time_max_iso: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 250,
) -> list[dict]:
    """List upcoming events from a calendar. Times must be ISO 8601 with tz."""
    svc = _service()
    params = {
        "calendarId": calendar_id,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if time_min_iso:
        params["timeMin"] = time_min_iso
    if time_max_iso:
        params["timeMax"] = time_max_iso
    if query:
        params["q"] = query
    return svc.events().list(**params).execute().get("items", [])


def create_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    location: str = "",
    description: str = "",
    attendees: Optional[list[dict]] = None,
    transparency: str = "opaque",
    calendar_id: str = "primary",
    timezone: str = "America/New_York",
    all_day: bool = False,
) -> dict:
    """Create a calendar event. Returns the created event resource.

    For all-day events, pass all_day=True with date-only strings
    (YYYY-MM-DD); end date is exclusive per the Google Calendar API.
    """
    if all_day:
        body = {
            "summary": summary,
            "start": {"date": start_iso},
            "end": {"date": end_iso},
            "transparency": transparency,
        }
    else:
        body = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": timezone},
            "end": {"dateTime": end_iso, "timeZone": timezone},
            "transparency": transparency,
        }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = attendees

    return _service().events().insert(
        calendarId=calendar_id,
        body=body,
        sendUpdates="none",
    ).execute()


def update_event(event_id: str, updates: dict, calendar_id: str = "primary") -> dict:
    """Patch an existing event with partial updates."""
    return _service().events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=updates,
        sendUpdates="none",
    ).execute()


def delete_event(event_id: str, calendar_id: str = "primary") -> None:
    """Delete an event by id."""
    _service().events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates="none",
    ).execute()


def find_event_by_title_and_date(
    title_substring: str,
    date_iso: str,
    calendar_id: str = "primary",
) -> Optional[dict]:
    """Find an event matching a title substring on a given date. For dedup."""
    from datetime import datetime, timedelta

    day = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    events = list_events(
        calendar_id=calendar_id,
        time_min_iso=start,
        time_max_iso=end,
    )
    title_lower = title_substring.lower()
    for ev in events:
        if title_lower in ev.get("summary", "").lower():
            return ev
    return None

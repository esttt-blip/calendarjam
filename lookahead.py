"""Week-ahead scanner — surfaces things to act on before they bite.

Four checks over the next 7 days (21 for birthdays/holidays):
  1. Collisions   — two timed events whose times overlap (can't be two places)
  2. Tight turns  — back-to-back events at different places with little/no gap
  3. Weather risk — outdoor events on days with heavy rain / storms
  4. Horizon      — upcoming birthdays + major holidays a few weeks out

Everything returns plain dicts; rendering lives in sync.py.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta, timezone

from calendar_api import _service
from weather import CANCELLATION_CODES, fetch_forecast

ET = zoneinfo.ZoneInfo("America/New_York")

US_HOLIDAYS_CAL = "en.usa#holiday@group.v.calendar.google.com"

# Only flag holidays people actually plan around
MAJOR_HOLIDAYS = (
    "new year", "martin luther king", "presidents", "memorial day", "juneteenth",
    "independence day", "labor day", "indigenous", "columbus", "veterans",
    "thanksgiving", "christmas", "halloween", "easter", "mother's day", "father's day",
    "valentine",
)

OUTDOOR_SIGNALS = (
    "viva", "ignite", "soccer", "baseball", "practice", "game", "spirit",
    "audi field", "park", "field", "outdoor", "tournament", "golf",
)

# Henry's activities — attended by him (and whichever parent handles the
# drive), not a "two places at once" clash with Esther's own commitments.
# Used to drop conflicts where different people in the house attend each event.
KID_SIGNALS = (
    "viva", "ignite", "soccer", "baseball", "swanson", "chelsea", "camp",
    "practice", "athletic development", "pitching", "catcher", "infield",
    "hitting", "academy night",
)


# ─────────────────────────── helpers ───────────────────────────


def _parse(ev: dict, which: str):
    """Return (datetime, is_all_day) for start/end. All-day → date at midnight ET."""
    node = ev.get(which, {})
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"]).astimezone(ET), False
    if "date" in node:
        d = datetime.fromisoformat(node["date"]).replace(tzinfo=ET)
        return d, True
    return None, False


def _is_drive(ev: dict) -> bool:
    return ev.get("summary", "").strip().startswith("🚗")


def _is_passive(ev: dict) -> bool:
    """Deliveries / free-time blocks that don't require you to 'be there' —
    not real two-places-at-once conflicts."""
    s = ev.get("summary", "").strip()
    if s.startswith(("🛒", "🍱", "📦")):
        return True
    return ev.get("transparency") == "transparent"


def _owner_class(ev: dict) -> str:
    """Who in the house this event belongs to. Henry's sports/school/camp are
    'kid'; everything else is 'adult'. A kid event overlapping an adult event
    isn't a two-places-at-once clash — different people attend each."""
    s = (ev.get("summary", "") or "").lower()
    return "kid" if any(k in s for k in KID_SIGNALS) else "adult"


def _same_title(a: dict, b: dict) -> bool:
    """True if two events share the same title — a duplicate, not a real
    conflict (avoids flagging an event against a stray copy of itself)."""
    return (a.get("summary", "") or "").strip().lower() == \
           (b.get("summary", "") or "").strip().lower()


# Tournament bracket placeholders — the team only plays one of these slots
# depending on earlier results, so overlapping bracket games aren't real
# conflicts. Recognized by TBD / bracket / round-name markers.
_BRACKET_MARKERS = ("tbd", "bracket", "consolation", "consolidation",
                    "semi final", "semifinal", "semi-final", "quarter final",
                    "quarterfinal", "quarter-final", "playoff", "play-in")


def _is_bracket_tbd(ev: dict) -> bool:
    s = (ev.get("summary", "") or "").lower()
    return any(m in s for m in _BRACKET_MARKERS)


def _is_outdoor(ev: dict) -> bool:
    blob = (ev.get("summary", "") + " " + ev.get("location", "")).lower()
    return any(s in blob for s in OUTDOOR_SIGNALS)


def _fetch_primary(days: int) -> list[dict]:
    svc = _service()
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    return svc.events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=250,
    ).execute().get("items", [])


# ─────────────────────────── checks ───────────────────────────


def find_collisions(events: list[dict]) -> list[dict]:
    """Pairs of timed events whose intervals overlap. Skips drive blocks
    overlapping their own trailing event, and all-day events."""
    # Exclude drive blocks — they're travel padding, and their adjacency to
    # the destination event would otherwise read as a false collision. The
    # real conflict between the two actual events is what matters.
    timed = []
    for e in events:
        start, all_day = _parse(e, "start")
        end, _ = _parse(e, "end")
        if (start and end and not all_day and not _is_drive(e)
                and not _is_passive(e) and not _is_bracket_tbd(e)):
            timed.append((start, end, e))
    timed.sort(key=lambda t: t[0])

    out = []
    for i in range(len(timed)):
        s1, e1, ev1 = timed[i]
        for j in range(i + 1, len(timed)):
            s2, e2, ev2 = timed[j]
            if s2 >= e1:
                break  # sorted: no later event can overlap
            # Duplicate of the same event (a stray copy) — not a real clash.
            if _same_title(ev1, ev2):
                continue
            # Different people in the house attend each (e.g. Henry's practice
            # vs Esther's evening plans) — not a two-places-at-once conflict.
            if _owner_class(ev1) != _owner_class(ev2):
                continue
            out.append({
                "a": ev1.get("summary", ""), "a_start": s1, "a_end": e1,
                "b": ev2.get("summary", ""), "b_start": s2, "b_end": e2,
                "day": s1.strftime("%a %b %-d"),
            })
    return out


def find_tight_turnarounds(events: list[dict], gap_min: int = 15) -> list[dict]:
    """Consecutive non-drive events at different locations with a small gap —
    you may not physically make the second one."""
    timed = []
    for e in events:
        start, all_day = _parse(e, "start")
        end, _ = _parse(e, "end")
        if start and end and not all_day and not _is_drive(e):
            timed.append((start, end, e))
    timed.sort(key=lambda t: t[0])

    out = []
    for i in range(len(timed) - 1):
        _, e1, ev1 = timed[i]
        s2, _, ev2 = timed[i + 1]
        gap = (s2 - e1).total_seconds() / 60
        loc1 = (ev1.get("location") or "").strip().lower()
        loc2 = (ev2.get("location") or "").strip().lower()
        # Different people attend (kid vs adult) — they don't share the drive,
        # so a tight gap between them isn't a turnaround problem for one person.
        if _owner_class(ev1) != _owner_class(ev2):
            continue
        if 0 <= gap <= gap_min and loc1 and loc2 and loc1 != loc2:
            out.append({
                "a": ev1.get("summary", ""), "a_end": e1,
                "b": ev2.get("summary", ""), "b_start": s2,
                "gap_min": int(gap),
                "day": e1.strftime("%a %b %-d"),
            })
    return out


def find_weather_risk(events: list[dict]) -> list[dict]:
    """Outdoor events on days with heavy rain or storms in the forecast."""
    forecast = fetch_forecast(days=7)
    if not forecast:
        return []
    out = []
    seen = set()
    for e in events:
        start, all_day = _parse(e, "start")
        if not start or all_day or not _is_outdoor(e) or _is_drive(e):
            continue
        day_key = start.strftime("%Y-%m-%d")
        fc = forecast.get(day_key)
        if not fc:
            continue
        risky = fc["precip_pct"] >= 60 or fc["weather_code"] in CANCELLATION_CODES
        if not risky:
            continue
        dedup = (e.get("summary", ""), day_key)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({
            "title": e.get("summary", ""),
            "day": start.strftime("%a %b %-d"),
            "time": start.strftime("%-I:%M %p"),
            "precip_pct": fc["precip_pct"],
            "emoji": fc["emoji"],
            "label": fc["label"],
        })
    return out


def find_horizon(days: int = 21) -> dict:
    """Upcoming birthdays (from primary, by title) + major holidays."""
    svc = _service()
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)

    # Birthdays: primary events whose title mentions birthday/bday
    primary = svc.events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=250,
    ).execute().get("items", [])
    birthdays = []
    for e in primary:
        title = e.get("summary", "")
        if "birthday" in title.lower() or "bday" in title.lower():
            start, _ = _parse(e, "start")
            if start:
                days_out = (start.date() - datetime.now(ET).date()).days
                birthdays.append({"title": title, "day": start.strftime("%a %b %-d"), "days_out": days_out})

    # Major holidays from the US holidays calendar
    holidays = []
    try:
        hol = svc.events().list(
            calendarId=US_HOLIDAYS_CAL, timeMin=now.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=50,
        ).execute().get("items", [])
        for e in hol:
            title = e.get("summary", "")
            if any(h in title.lower() for h in MAJOR_HOLIDAYS):
                start, _ = _parse(e, "start")
                if start:
                    days_out = (start.date() - datetime.now(ET).date()).days
                    holidays.append({"title": title, "day": start.strftime("%a %b %-d"), "days_out": days_out})
    except Exception as e:
        print(f"  [lookahead] holiday fetch failed: {e}")

    return {"birthdays": birthdays, "holidays": holidays}


def scan(days: int = 7) -> dict:
    """Run all checks. Returns a dict the email renderer consumes."""
    events = _fetch_primary(days)
    return {
        "collisions": find_collisions(events),
        "tight": find_tight_turnarounds(events),
        "weather": find_weather_risk(events),
        "horizon": find_horizon(21),
    }


if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    import json
    result = scan()
    print(json.dumps(result, indent=2, default=str))

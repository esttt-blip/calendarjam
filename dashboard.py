"""Dashboard snapshot generator.

The cloud sync has Google Calendar access; the Streamlit app does not (by
design — keeps the app fast and credential-light). So sync.py calls
build_snapshot() each run and commits dashboard.json to the repo. The app
reads that snapshot to render the command station.
"""

from __future__ import annotations

import json
import re
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lookahead
from calendar_api import list_events
from weather import fetch_today_forecast

BASE_DIR = Path(__file__).parent
DASHBOARD_FILE = BASE_DIR / "dashboard.json"
ACTIVITY_LOG = BASE_DIR / "activity_log.json"
ET = zoneinfo.ZoneInfo("America/New_York")

OUTDOOR_SIGNALS = (
    "viva", "ignite", "soccer", "baseball", "practice", "game", "spirit",
    "audi field", "park", "field", "outdoor", "tournament", "golf",
)


def _is_outdoor(summary: str, location: str) -> bool:
    blob = (summary + " " + location).lower()
    return any(s in blob for s in OUTDOOR_SIGNALS)


def _clean(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _today_events() -> list[dict]:
    now = datetime.now(ET)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    raw = list_events(
        time_min_iso=start.isoformat(),
        time_max_iso=end.isoformat(),
        max_results=50,
    )
    out = []
    for e in raw:
        s = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
        if s and "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)
            time_str = dt.strftime("%-I:%M %p")
            sort_key = dt.isoformat()
        else:
            time_str = "All day"
            sort_key = (s or "") + "T00:00"
        summary = e.get("summary", "(untitled)")
        location = e.get("location", "")
        out.append({
            "time": time_str,
            "title": summary,
            "location": location,
            "description": _clean(e.get("description", ""), 140),
            "outdoor": _is_outdoor(summary, location),
            "is_drive": summary.strip().startswith("🚗"),
            "sort": sort_key,
        })
    out.sort(key=lambda x: x["sort"])
    return out


def _added_this_week() -> int:
    if not ACTIVITY_LOG.exists():
        return 0
    try:
        entries = json.loads(ACTIVITY_LOG.read_text())
    except Exception:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    n = 0
    for e in entries:
        try:
            if e.get("action") in ("added", "acked") and \
               datetime.fromisoformat(e["ts"]).timestamp() > cutoff:
                n += 1
        except Exception:
            continue
    return n


def _serialize_scan(scan: dict) -> dict:
    """Convert datetime objects in the lookahead scan to display strings."""
    collisions = []
    for c in scan.get("collisions", []):
        collisions.append({
            "day": c["day"],
            "a": c["a"], "a_time": c["a_start"].strftime("%-I:%M %p"),
            "b": c["b"], "b_time": c["b_start"].strftime("%-I:%M %p"),
        })
    tight = []
    for t in scan.get("tight", []):
        tight.append({
            "day": t["day"], "a": t["a"], "b": t["b"], "gap_min": t["gap_min"],
        })
    return {
        "collisions": collisions,
        "tight": tight,
        "weather": scan.get("weather", []),
        "horizon": scan.get("horizon", {"birthdays": [], "holidays": []}),
    }


def build_snapshot() -> dict:
    """Build and write dashboard.json. Returns the snapshot dict."""
    now = datetime.now(ET)
    today_events = _today_events()
    weather = fetch_today_forecast()

    try:
        scan = _serialize_scan(lookahead.scan(days=7))
    except Exception as e:
        print(f"  [dashboard] lookahead failed: {e}")
        scan = {"collisions": [], "tight": [], "weather": [], "horizon": {"birthdays": [], "holidays": []}}

    n_conflicts = len(scan["collisions"]) + len(scan["tight"])

    snapshot = {
        "generated_at": now.isoformat(),
        "generated_label": now.strftime("%a %b %-d, %-I:%M %p"),
        "weather": weather,
        "today": [e for e in today_events if not e["is_drive"]],
        "today_with_drives": today_events,
        "week_ahead": scan,
        "counts": {
            "events_today": len([e for e in today_events if not e["is_drive"]]),
            "conflicts_week": n_conflicts,
            "added_week": _added_this_week(),
        },
    }

    DASHBOARD_FILE.write_text(json.dumps(snapshot, indent=2, default=str))
    return snapshot


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    snap = build_snapshot()
    print(f"Dashboard written: {snap['counts']}")

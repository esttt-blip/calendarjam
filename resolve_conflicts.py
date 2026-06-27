"""Process conflict-deletion requests queued by the dashboard.

The app writes requests to conflict_deletes.json:
  [{"date": "2026-07-01", "title": "Keith's birthday"}, ...]
This finds each matching calendar event (by title + date) and deletes it, then
clears the queue. Runs on a short schedule + manual dispatch. Isolated from the
daily sync so it can never affect the morning email.
"""

from __future__ import annotations

import json
from pathlib import Path

from calendar_api import delete_event, find_event_by_title_and_date

BASE = Path(__file__).parent
QUEUE = BASE / "conflict_deletes.json"


def main() -> None:
    if not QUEUE.exists():
        print("No conflict_deletes.json — nothing to do.")
        return
    try:
        requests = json.loads(QUEUE.read_text())
    except Exception:
        requests = []
    if not requests:
        print("Queue empty.")
        return

    remaining = []
    for r in requests:
        date, title = r.get("date"), r.get("title")
        if not date or not title:
            continue
        try:
            ev = find_event_by_title_and_date(title, f"{date}T12:00:00+00:00")
            if ev:
                delete_event(ev["id"])
                print(f"deleted: {title} on {date} ({ev['id']})")
            else:
                print(f"not found (skipping): {title} on {date}")
        except Exception as e:
            print(f"failed {title} {date}: {e}")
            remaining.append(r)  # keep for retry next run

    QUEUE.write_text(json.dumps(remaining, indent=2))
    print(f"Done. {len(requests) - len(remaining)} processed, {len(remaining)} retained.")


if __name__ == "__main__":
    main()

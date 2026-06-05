"""Activity log — append-only record of every action calendarjam takes.

Each entry: timestamp, action, title, source. Used to render the
'recent activity' section in the daily email and (optionally) a history
tab in the Streamlit app.

Local-only writes. The cloud runs commit activity_log.json back via the
GitHub Actions checkout/push flow. The Streamlit app writes via the
GitHub Contents API (see app/app.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

BASE_DIR = Path(__file__).parent
ACTIVITY_LOG = BASE_DIR / "activity_log.json"
MAX_ENTRIES = 500  # ~3 months at typical volume

Action = Literal["added", "dismissed", "acked", "skipped"]


def _load() -> list[dict]:
    if not ACTIVITY_LOG.exists():
        return []
    try:
        return json.loads(ACTIVITY_LOG.read_text())
    except Exception:
        return []


def append(action: Action, title: str, source: str = "", **extra) -> None:
    """Append one entry. Auto-trims to MAX_ENTRIES."""
    entries = _load()
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "title": title[:120],
        "source": source,
        **extra,
    })
    entries = entries[-MAX_ENTRIES:]
    ACTIVITY_LOG.write_text(json.dumps(entries, indent=2))


def recent(days: int = 5) -> list[dict]:
    """Return entries from the last N days, newest first."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out = []
    for e in _load():
        try:
            if datetime.fromisoformat(e["ts"]).timestamp() > cutoff:
                out.append(e)
        except Exception:
            continue
    return sorted(out, key=lambda e: e["ts"], reverse=True)

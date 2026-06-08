"""Delivery detection — turn delivery emails into calendar entries.

- MightyMeals: gives a delivery DATE only → all-day event (top of the day).
- Walmart grocery: gives a delivery WINDOW (e.g. 7pm–9pm) + date → timed event.
  Walmart's emails are varied (shipped pkgs, 'delivered' receipts, order
  changes); we only act on clear delivery-window confirmations and skip the rest.

Returned dicts are consumed by sync.py, which creates the calendar events.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

MONTHS = ("january february march april may june july august september "
          "october november december").split()
_MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
_MONTH_NUM.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})


def _to_date(month_word: str, day: str, year: str | None) -> str | None:
    mn = _MONTH_NUM.get(month_word.lower())
    if not mn:
        return None
    y = int(year) if year else datetime.now().year
    try:
        return datetime(y, mn, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─────────────────────────── MightyMeals ───────────────────────────


def parse_mightymeals(subject: str, body: str) -> dict | None:
    # "Your order will arrive on June 10, 2026"
    m = re.search(r"arrive on\s+([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})?", body, re.I)
    if not m:
        return None
    date = _to_date(m.group(1), m.group(2), m.group(3))
    if not date:
        return None
    end = (datetime.fromisoformat(date) + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "kind": "allday",
        "title": "🍱 MightyMeals delivery",
        "start": date,
        "end": end,
        "description": "MightyMeals delivery (auto-added from order email).",
        "dedup": f"mightymeals-{date}",
    }


# ─────────────────────────── Walmart ───────────────────────────

_SKIP_WALMART = ("delivered:", "we're confirming", "order changes", "shipped:",
                 "thanks for shopping", "your refund")


def parse_walmart(subject: str, body: str) -> dict | None:
    subj = subject.lower()
    if any(s in subj for s in _SKIP_WALMART):
        return None

    # Walmart's grocery confirmation says it cleanly in one phrase:
    #   "Arrives Mon, Jun 8, 7pm - 9pm"
    m = re.search(
        r"arrives\s+\w+,\s+([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{4})?,?\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*([ap])m\s*(?:-|–|to)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*([ap])m",
        body, re.I,
    )
    if not m:
        return None

    date = _to_date(m.group(1), m.group(2), m.group(3))
    if not date:
        return None

    def _t(h, mn, ap):
        h = int(h) % 12
        if ap.lower() == "p":
            h += 12
        return f"{date}T{h:02d}:{int(mn or 0):02d}:00"

    return {
        "kind": "timed",
        "title": "🛒 Walmart delivery",
        "start": _t(m.group(4), m.group(5), m.group(6)),
        "end": _t(m.group(7), m.group(8), m.group(9)),
        "description": "Walmart delivery window (auto-added from order email).",
        "dedup": f"walmart-{date}-{m.group(4)}{m.group(6)}",
    }


_PARSERS = {
    "mightymeals": parse_mightymeals,
    "walmart": parse_walmart,
}


def detect(from_addr: str, subject: str, body: str) -> dict | None:
    """Return a delivery dict if this email is a recognizable delivery, else None."""
    f = (from_addr or "").lower()
    for sender, parser in _PARSERS.items():
        if sender in f:
            return parser(subject, body)
    return None

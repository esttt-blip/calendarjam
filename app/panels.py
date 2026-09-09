"""calendarjam agent panels — trip planner + deal hunter.

These two render side by side under Today. Both are defensive: bad or missing
data yields a quiet placeholder rather than taking the page down.

The trip planner lists upcoming trips. (Flight fare-watching was removed once
the Italy flights were booked — trips now render as simple cards.)
"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

WATCHED_VENDORS = ("Bombas", "OOFOS", "Cotopaxi", "Bad Birdie", "G/FORE",
                   "NOBULL", "Away", "P.F. Candle Co")


# ─────────────────────────── trips ───────────────────────────


def upcoming_trips(trips: list[dict], today: date | None = None) -> list[dict]:
    """Trips whose end date hasn't passed. Undated trips always show."""
    today = today or date.today()
    out = []
    for t in trips:
        end = t.get("end")
        if not end:
            out.append(t)
            continue
        try:
            if datetime.fromisoformat(str(end)[:10]).date() >= today:
                out.append(t)
        except Exception:
            out.append(t)
    return out


def render_trip_planner(trips: list[dict], *_ignored) -> None:
    """Upcoming trips, rendered as simple cards.

    Accepts and ignores extra positional args for backward compatibility with
    older callers that passed agents_data / alerts for fare watching.
    """
    st.markdown("<div class='sec'>✈️ Trips</div>", unsafe_allow_html=True)

    live = upcoming_trips(trips)
    if not live:
        st.caption("No upcoming trips.")
        return

    for t in live:
        with st.container(border=True):
            st.markdown(
                f"<div class='trip-title'>{t.get('title','Trip')} "
                f"<span class='muted'>· {t.get('window','')}</span></div>"
                f"<div class='trip-detail'>{t.get('detail','')}</div>",
                unsafe_allow_html=True,
            )


# ─────────────────────────── deal hunter ───────────────────────────


def _vendor_of(p: dict) -> str:
    blob = f"{p.get('id','')} {p.get('url','')} {p.get('name','')}".lower()
    for v in WATCHED_VENDORS:
        if v.lower() in blob:
            return v
    return (p.get("name") or "Tracked").split()[0]


def render_deal_hunter(shopping: dict | None) -> None:
    """Standing vendor watchlist; tracked items show price and any markdown."""
    products = (shopping or {}).get("products", [])
    any_deal = any(p.get("status", {}).get("is_deal") for p in products)
    head = "🏷️ Deal hunter — on sale now" if any_deal else "🏷️ Deal hunter"
    st.markdown(f"<div class='sec'>{head}</div>", unsafe_allow_html=True)

    by_vendor: dict[str, list[dict]] = {v: [] for v in WATCHED_VENDORS}
    for p in products:
        by_vendor.setdefault(_vendor_of(p), []).append(p)

    with st.container(border=True):
        for i, (vendor, items) in enumerate(by_vendor.items()):
            first = " first" if i == 0 else ""
            if not items:
                st.markdown(
                    f"<div class='deal-row{first}'><div class='deal-name'>{vendor}</div>"
                    f"<span class='muted'>watching · nothing tracked yet</span></div>",
                    unsafe_allow_html=True,
                )
                continue
            for p in items:
                s = p.get("status", {})
                pct = s.get("pct_off", 0)
                stock = "in stock" if s.get("in_stock") else "⚠️ out of stock"
                if s.get("is_deal"):
                    badge = (f"<span class='deal-badge'>&minus;{pct}%</span>"
                             if pct else "")
                    was = (f"<span class='deal-was'>was ${s['msrp']:,.2f}</span>"
                           if s.get("msrp") and s.get("price")
                           and s["price"] < s["msrp"] else "")
                    st.markdown(
                        f"<div class='deal-row{first}'>"
                        f"<div class='deal-name'>{vendor} · {p.get('name','')}</div>"
                        f"<div class='deal-price'>${s.get('price',0):,.2f}{was}{badge}</div>"
                        f"<a class='deal-link' href='{p.get('buy_url','#')}' "
                        f"target='_blank'>View &rarr;</a>"
                        f"<span class='muted'> · {stock}</span></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div class='deal-row{first}'>"
                        f"<div class='deal-name'>{vendor} · {p.get('name','')}</div>"
                        f"<span class='muted'>${s.get('price',0):,.2f} · no markdown "
                        f"(low ${s.get('msrp',0):,.2f})</span></div>",
                        unsafe_allow_html=True,
                    )

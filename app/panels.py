"""calendarjam agent panels — trip planner (with fare watch) + deal hunter.

These two render side by side under Today. Both are defensive: bad or missing
data yields a quiet placeholder rather than taking the page down.

The trip planner is the flight tracker: a trip binds to a flight-watch agent in
agents.json via its "agent_id", so fares render inside the trip they belong to
instead of in a separate section.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

WATCHED_VENDORS = ("Bombas", "OOFOS", "Cotopaxi")
CABINS = ("economy", "business")


# ─────────────────────────── trips + fares ───────────────────────────


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


def _agent_for(agents_data: dict | None, agent_id: str | None) -> dict | None:
    if not agent_id:
        return None
    for ag in (agents_data or {}).get("agents", []):
        if ag.get("id") == agent_id:
            return ag
    return None


def _render_fares(agent: dict, alerts: list[dict]) -> None:
    """Cheapest fare, any drops, the route table, and price history."""
    stt = agent.get("status") or {}
    cfg = agent.get("config") or {}
    pax = cfg.get("travelers", 1) or 1

    if stt.get("state") != "live":
        st.caption(stt.get("note", "Flight watch not active yet."))
        return

    cp, ce = stt.get("cheapest_plan"), stt.get("cheapest_econ")
    if cp and ce:
        st.markdown(
            f"<div style='font-size:13.5px;margin:2px 0 1px'>Cheapest now: "
            f"<b>{cp}</b> — <b>${ce:,.0f}</b> economy "
            f"<span style='color:#9a9aa7'>(≈${ce/pax:,.0f}/person)</span></div>",
            unsafe_allow_html=True,
        )

    # app.py's flight_price_alerts() doesn't tag agent_id (single agent today),
    # so untagged alerts are treated as belonging to this agent.
    mine = [a for a in (alerts or [])
            if a.get("agent_id") in (None, agent.get("id"))]
    for a in mine[:3]:
        badge = ("<span class='falert-badge'>new low</span>"
                 if a.get("kind") == "low" else "")
        st.markdown(
            f"<div style='font-size:12.5px;padding:1px 0'>{a['label']} — "
            f"<span class='falert-drop'>▼ ${a['drop']:,.0f}</span> to "
            f"<b>${a['val']:,.0f}</b>{badge}</div>",
            unsafe_allow_html=True,
        )

    hist = agent.get("history", [])
    rows = ""
    for ri, res in enumerate(stt.get("results", [])):
        lab = res.get("label", "—")
        band = "ftbl-b0" if ri % 2 == 0 else "ftbl-b1"
        for cabin in CABINS:
            cd = res.get(cabin) or {}
            cur = cd.get("low")
            if cur is None:
                continue
            vals = [h.get(f"{lab} {cabin}") for h in hist if h.get(f"{lab} {cabin}")]
            lo = min(vals) if vals else cur
            cls = "ftbl-best" if (cabin == "economy" and lab == cp) else band
            rows += (f"<tr class='{cls}'><td>{lab}</td><td>{cabin.title()}</td>"
                     f"<td>${cur:,.0f}</td><td>${lo:,.0f}</td></tr>")
    if rows:
        st.markdown(
            "<table class='ftbl'><thead><tr><th>Route</th><th>Cabin</th>"
            f"<th>Today</th><th>Low seen</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<div class='muted' style='margin-top:5px'>Total for {pax} · "
            f"{cfg.get('airline_label','')}</div>",
            unsafe_allow_html=True,
        )

    if len(hist) >= 2:
        try:
            df = pd.DataFrame(hist)
            if "date" in df.columns:
                df = df.set_index("date")
            st.markdown(
                f"<div class='muted' style='margin:10px 0 2px'>📈 Since "
                f"{hist[0].get('date','—')} · {len(hist)} checks</div>",
                unsafe_allow_html=True,
            )
            st.line_chart(df, height=190)
        except Exception:
            pass


def render_trip_planner(trips: list[dict], agents_data: dict | None,
                        alerts: list[dict] | None = None) -> None:
    """Upcoming trips; a trip bound to a flight agent shows its fares inline."""
    st.markdown("<div class='sec'>✈️ Trips &amp; fare watch</div>",
                unsafe_allow_html=True)

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
            agent = _agent_for(agents_data, t.get("agent_id"))
            if agent:
                _render_fares(agent, alerts or [])


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

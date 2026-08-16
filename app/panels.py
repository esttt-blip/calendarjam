"""calendarjam side panels — fare watch + deals worth a look.

Rendered on the command station below the week grid. Both panels read JSON
that the scheduled agents commit to the repo:
  - agents.json    (agents.py — Italy flight-watch, via SerpApi)
  - shopping.json  (shopping.py — Shopify price watch)

Every panel is defensive: if its data file is missing, empty, or malformed it
renders a quiet placeholder instead of taking the whole page down.
"""

from __future__ import annotations

import streamlit as st

# Vendors Esther wants surfaced even when nothing is tracked or discounted,
# so the panel is a standing list rather than appearing only on a hit.
WATCHED_VENDORS = ("Bombas", "OOFOS", "Cotopaxi")

CABINS = ("economy", "business")


def _money(value, decimals: int = 0) -> str:
    try:
        return f"${float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _vendor_of(product: dict) -> str:
    """Best-effort vendor name from a shopping.json entry."""
    blob = f"{product.get('id', '')} {product.get('url', '')}".lower()
    for vendor in WATCHED_VENDORS:
        if vendor.lower() in blob:
            return vendor
    return (product.get("name") or "Tracked item").split()[0]


# ─────────────────────────── fare drop ───────────────────────────


def _fare_history_chart(agent: dict) -> None:
    history = agent.get("history") or []
    if len(history) < 2:
        return
    keys: list[str] = []
    for snap in history:
        for k in snap:
            if k != "date" and k not in keys:
                keys.append(k)
    if not keys:
        return
    data = {"date": [s.get("date", "") for s in history]}
    for k in keys:
        data[k] = [s.get(k) for s in history]
    st.line_chart(data, x="date", y=keys, height=240)


def render_fare_panel(fetch_file) -> None:
    """Cheapest tracked itinerary, a plan x cabin table, and price history."""
    st.markdown("#### ✈️ Fare drop")
    try:
        data, _ = fetch_file("agents.json")
    except Exception:
        data = None

    agents = [a for a in (data or {}).get("agents", [])
              if a.get("type") == "flight-multicity"]
    if not agents:
        st.caption("No flight watch configured.")
        return

    for agent in agents:
        status = agent.get("status") or {}
        config = agent.get("config") or {}

        st.markdown(f"**{agent.get('name', '✈️ Flight watch')}**")
        if config.get("route_note"):
            st.caption(config["route_note"])

        if status.get("state") == "awaiting_key":
            st.info(status.get("note", "Agent is idle."), icon="🔑")
            continue

        cheapest_plan = status.get("cheapest_plan")
        cheapest_econ = status.get("cheapest_econ")
        if cheapest_plan:
            st.metric(
                label=f"Cheapest economy · {cheapest_plan}",
                value=_money(cheapest_econ),
            )

        rows = []
        for result in status.get("results", []):
            row = {"Option": result.get("label", "—")}
            for cabin in CABINS:
                leg = result.get(cabin) or {}
                row[cabin.title()] = _money(leg.get("low"))
                flights = leg.get("flight_numbers") or []
                row[f"{cabin.title()} flights"] = " / ".join(flights) if flights else "—"
            rows.append(row)
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)

        _fare_history_chart(agent)

        if status.get("updated"):
            st.caption(f"Fares updated {status['updated']} · {config.get('airline_label', '')}")


# ─────────────────────────── deals worth a look ───────────────────────────


def render_deals_panel(fetch_file) -> None:
    """Standing list of watched vendors, with tracked prices and live deals."""
    st.markdown("#### 🏷️ Deals worth a look")
    try:
        data, _ = fetch_file("shopping.json")
    except Exception:
        data = None

    products = (data or {}).get("products", [])
    by_vendor: dict[str, list[dict]] = {v: [] for v in WATCHED_VENDORS}
    for product in products:
        by_vendor.setdefault(_vendor_of(product), []).append(product)

    live_deals = sum(
        1 for p in products if (p.get("status") or {}).get("is_deal")
    )
    if not live_deals:
        st.caption("No active sales at your vendors right now — still watching.")

    for vendor, items in by_vendor.items():
        with st.container(border=True):
            if not items:
                st.markdown(
                    f"<span style='font-size:13.5px;color:#888'>👀 <b>{vendor}</b>"
                    f" — watching, nothing tracked yet</span>",
                    unsafe_allow_html=True,
                )
                continue

            st.markdown(f"**{vendor}**")
            for product in items:
                status = product.get("status") or {}
                name = product.get("name", "Tracked item")
                price = _money(status.get("price"), 2)
                msrp = _money(status.get("msrp"), 2)
                stock = "in stock" if status.get("in_stock") else "out of stock"

                if status.get("is_deal"):
                    st.markdown(
                        f"<div style='font-size:13.5px'>🔥 <b>{name}</b> — "
                        f"<b>{price}</b> <span style='color:#999;"
                        f"text-decoration:line-through'>{msrp}</span> "
                        f"<span style='color:#c0392b;font-weight:700'>"
                        f"-{status.get('pct_off', 0)}%</span> · {stock}</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='font-size:13.5px;color:#555'>• {name} — "
                        f"{price} (no markdown) · {stock}</div>",
                        unsafe_allow_html=True,
                    )
                if product.get("buy_url"):
                    st.caption(f"[View product]({product['buy_url']})")

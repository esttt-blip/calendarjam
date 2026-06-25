"""calendarjam agents — autonomous monitors that write status to agents.json.

Currently: Italy flight-watch (multi-city, via SerpApi Google Flights). Runs on
a schedule (.github/workflows/agents.yml), no Claude needed. Activate by adding
a SERPAPI_KEY repo secret.

Each run logs the day's lowest fare per plan + cabin into the agent's history,
so the dashboard can show today's price against the range we've observed.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import requests

BASE = Path(__file__).parent
AGENTS_FILE = BASE / "agents.json"
SERP_KEY = os.getenv("SERPAPI_KEY")
SERP_URL = "https://serpapi.com/search.json"

CABIN_CLASS = {"economy": "1", "business": "3"}  # SerpApi travel_class codes


def _today() -> str:
    return datetime.date.today().isoformat()


def _search(plan: dict, travel_class: str, adults: int, include_airlines: str):
    """One multi-city Google Flights search via SerpApi.

    Returns (low_price, top_airline, insights) — any may be None on failure or
    when the route returns nothing for the airline filter.
    """
    legs = [{"departure_id": l["from"], "arrival_id": l["to"], "date": l["date"]}
            for l in plan["legs"]]
    params = {
        "engine": "google_flights",
        "type": "3",  # multi-city
        "multi_city_json": json.dumps(legs),
        "adults": str(adults),
        "travel_class": travel_class,
        "include_airlines": include_airlines,
        "currency": "USD", "hl": "en", "gl": "us",
        "api_key": SERP_KEY,
    }
    try:
        resp = requests.get(SERP_URL, params=params, timeout=45)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    search failed [{plan['label']}/{travel_class}]: {e}")
        return None, None, None

    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    priced = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not priced:
        return None, None, data.get("price_insights")
    cheapest = min(priced, key=lambda f: f["price"])
    # airline name of the cheapest itinerary's first leg
    airline = None
    legs_detail = cheapest.get("flights") or []
    if legs_detail and isinstance(legs_detail[0], dict):
        airline = legs_detail[0].get("airline")
    return cheapest["price"], airline, data.get("price_insights")


def run_flight_agent(agent: dict) -> None:
    cfg = agent["config"]
    if not SERP_KEY:
        agent["status"] = {
            "state": "awaiting_key",
            "note": "Add a SERPAPI_KEY repo secret to activate.",
            "updated": None,
        }
        print("  SERPAPI_KEY not set — agent idle.")
        return

    adults = cfg.get("travelers", 1)
    include = cfg.get("include_airlines", "STAR_ALLIANCE")
    cabins = cfg.get("cabins", ["economy"])

    results = []
    for plan in cfg["plans"]:
        entry = {"label": plan["label"], "cabins": {}}
        for cabin in cabins:
            tclass = CABIN_CLASS.get(cabin, "1")
            low, airline, insights = _search(plan, tclass, adults, include)
            entry["cabins"][cabin] = {"low": low, "airline": airline,
                                      "is_united": bool(airline and "united" in airline.lower())}
            typ = (insights or {}).get("typical_price_range")
            if typ:
                entry["cabins"][cabin]["typical_range"] = typ
            print(f"  {plan['label']} / {cabin}: {low} ({airline})")
        results.append(entry)

    econ = [(e["label"], e["cabins"].get("economy", {}).get("low"))
            for e in results if e["cabins"].get("economy", {}).get("low")]
    cheapest = min(econ, key=lambda x: x[1]) if econ else (None, None)

    agent["status"] = {
        "state": "live",
        "updated": _today(),
        "cheapest_plan": cheapest[0],
        "cheapest_econ": cheapest[1],
        "results": results,
    }

    snap = {"date": _today()}
    for e in results:
        for cabin in ("economy", "business"):
            v = e["cabins"].get(cabin, {}).get("low")
            if v:
                snap[f"{e['label']} {cabin}"] = v
    if len(snap) > 1:
        agent.setdefault("history", []).append(snap)
        agent["history"] = agent["history"][-180:]


def main() -> None:
    data = json.loads(AGENTS_FILE.read_text()) if AGENTS_FILE.exists() else {"agents": []}
    for agent in data.get("agents", []):
        if agent.get("type") == "flight-multicity":
            print(f"Running agent: {agent['id']}")
            run_flight_agent(agent)
    AGENTS_FILE.write_text(json.dumps(data, indent=2))
    print("agents.json updated.")


if __name__ == "__main__":
    main()

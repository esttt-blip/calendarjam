"""calendarjam agents — autonomous monitors that write status to agents.json.

Italy flight-watch: compares the date options for IAD→Rome / Munich→IAD
(multi-city, via SerpApi Google Flights). Captures the cheapest itinerary's
flight numbers and logs each run so the dashboard can show our tracked range.
Runs on a schedule (.github/workflows/agents.yml). Activate with a SERPAPI_KEY
repo secret.
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
CABIN_CLASS = {"economy": "1", "business": "3"}


def _today() -> str:
    return datetime.date.today().isoformat()


def _search(plan: dict, travel_class: str, adults: int, include_airlines: str):
    """One multi-city Google Flights search. Returns a dict for the cheapest
    itinerary (price, flight numbers, airlines, layovers) or None."""
    legs = [{"departure_id": l["from"], "arrival_id": l["to"], "date": l["date"]}
            for l in plan["legs"]]
    params = {
        "engine": "google_flights",
        "type": "3",
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
        return None

    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    priced = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not priced:
        return None
    c = min(priced, key=lambda f: f["price"])
    segs = c.get("flights") or []
    flight_numbers, airlines = [], []
    for s in segs:
        if s.get("flight_number"):
            flight_numbers.append(s["flight_number"])
        if s.get("airline"):
            airlines.append(s["airline"])
    return {
        "low": c["price"],
        "flight_numbers": flight_numbers,
        "airlines": airlines,
        "is_united": any("united" in (a or "").lower() for a in airlines),
        "layovers": len(c.get("layovers") or []),
        "insights": data.get("price_insights"),
    }


def run_flight_agent(agent: dict) -> None:
    cfg = agent["config"]
    if not SERP_KEY:
        agent["status"] = {"state": "awaiting_key",
                           "note": "Add a SERPAPI_KEY repo secret to activate.",
                           "updated": None}
        print("  SERPAPI_KEY not set — idle.")
        return

    adults = cfg.get("travelers", 1)
    include = cfg.get("include_airlines", "STAR_ALLIANCE")
    cabins = cfg.get("cabins", ["economy"])

    results = []
    for plan in cfg["plans"]:
        entry = {"label": plan["label"]}
        for cabin in cabins:
            res = _search(plan, CABIN_CLASS.get(cabin, "1"), adults, include)
            if res:
                entry[cabin] = res
                print(f"  {plan['label']} / {cabin}: ${res['low']} "
                      f"{'/'.join(res['flight_numbers'])}")
            else:
                print(f"  {plan['label']} / {cabin}: no fare")
        results.append(entry)

    econ = [(e["label"], e["economy"]["low"]) for e in results if e.get("economy")]
    cheapest = min(econ, key=lambda x: x[1]) if econ else (None, None)

    agent["status"] = {"state": "live", "updated": _today(),
                       "cheapest_plan": cheapest[0], "cheapest_econ": cheapest[1],
                       "results": results}

    snap = {"date": _today()}
    for e in results:
        if e.get("economy"):
            snap[f"{e['label']} economy"] = e["economy"]["low"]
        if e.get("business"):
            snap[f"{e['label']} business"] = e["business"]["low"]
    if len(snap) > 1:
        hist = agent.setdefault("history", [])
        if hist and hist[-1].get("date") == snap["date"]:
            hist[-1] = snap  # same-day rerun → replace, don't double-log
        else:
            hist.append(snap)
        agent["history"] = hist[-180:]


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

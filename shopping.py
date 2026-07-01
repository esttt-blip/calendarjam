"""calendarjam shopping monitor — watches product prices at retailer sites and
flags when one drops below its tracked baseline.

Currently supports Shopify storefronts via their public product JSON endpoint
(e.g. https://www.oofos.com/products/<handle>.json). No API key, no SerpApi
budget — a plain fetch — so it's safe to run daily.

For each product it records today's price + stock, keeps a price history, and
sets status.is_deal when the current price is below the tracked MSRP (or the
site exposes a compare-at markdown). The dashboard renders the deals cell only
when at least one product is a deal.

Add a product by appending an entry to shopping.json:
  {
    "id": "unique-slug",
    "name": "Human label",
    "source": "shopify",
    "url": "https://<store>/products/<handle>.json",
    "match": {"size": "8", "color": "White Thermo"},   # color optional
    "buy_url": "https://<store>/products/<handle>"
  }
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import requests

BASE = Path(__file__).parent
FILE = BASE / "shopping.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; calendarjam/1.0)"}


def _today() -> str:
    return datetime.date.today().isoformat()


def _find_variant(product: dict, match: dict):
    """Pick the variant matching the requested size (and color, if given)."""
    size = str(match.get("size", "")).strip()
    color = (match.get("color") or "").strip().lower()
    for v in product.get("variants", []):
        opt_size = str(v.get("option1", "")).strip()
        opt_color = (v.get("option2") or "").strip().lower()
        if opt_size == size and (not color or color in opt_color):
            return v
    return None


def check_shopify(prod: dict):
    r = requests.get(prod["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    product = r.json().get("product", {})
    v = _find_variant(product, prod.get("match", {}))
    if not v:
        return None
    cmp_raw = v.get("compare_at_price") or ""
    return {
        "price": float(v["price"]),
        "compare_at": float(cmp_raw) if cmp_raw not in ("", None) else None,
        "in_stock": (v.get("inventory_quantity", 0) > 0)
        or v.get("inventory_policy") == "continue",
    }


def update(prod: dict) -> None:
    source = prod.get("source", "shopify")
    try:
        res = check_shopify(prod) if source == "shopify" else None
    except Exception as e:
        print(f"  {prod['id']}: fetch failed: {e}")
        return
    if not res:
        print(f"  {prod['id']}: variant not found / no data")
        return

    st = prod.setdefault("status", {})
    # Baseline = the highest price we've ever seen (or a marked-up compare-at).
    # A drop below it — or an active compare-at markdown — counts as a deal.
    msrp = max(st.get("msrp") or res["price"], res["price"], res["compare_at"] or 0)
    is_deal = (res["compare_at"] is not None and res["price"] < res["compare_at"]) \
        or (res["price"] < msrp)
    pct_off = round(100 * (msrp - res["price"]) / msrp) if msrp and res["price"] < msrp else 0

    st.update({
        "updated": _today(),
        "price": res["price"],
        "msrp": msrp,
        "compare_at": res["compare_at"],
        "in_stock": res["in_stock"],
        "is_deal": bool(is_deal),
        "pct_off": pct_off,
    })

    hist = prod.setdefault("history", [])
    snap = {"date": _today(), "price": res["price"]}
    if hist and hist[-1].get("date") == snap["date"]:
        hist[-1] = snap  # same-day rerun → replace, don't double-log
    else:
        hist.append(snap)
    prod["history"] = hist[-365:]

    print(f"  {prod['id']}: ${res['price']} (msrp ${msrp})"
          f"{'  DEAL -' + str(pct_off) + '%' if is_deal else ''}")


def main() -> None:
    data = json.loads(FILE.read_text()) if FILE.exists() else {"products": []}
    for prod in data.get("products", []):
        print(f"Checking {prod['id']}")
        update(prod)
    FILE.write_text(json.dumps(data, indent=2))
    print("shopping.json updated.")


if __name__ == "__main__":
    main()

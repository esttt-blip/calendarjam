"""calendarjam — command station.

Mobile-first daily dashboard with collapsible planning sections. Priority:
attention → today → decisions → look-ahead → the week, then collapsible
Trips / Agents / To-do / Standing-items (clean on phone, expandable on desktop).

Reads from the repo:
  - dashboard.json        (week agenda + horizon, written by sync)
  - pending_events.json   (review queue — interactive)
  - app_open_items.json   (to-do checklist — interactive)
  - activity_log.json     (recent decisions)

Daily insights come from a rules engine; `_llm_insights` is the hook to swap in
a real LLM later (add ANTHROPIC_API_KEY + implement that one function).

Streamlit Cloud secrets: GITHUB_TOKEN, GITHUB_REPO.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
import panels

REPO = st.secrets.get("GITHUB_REPO", "esttt-blip/calendarjam")
TOKEN = st.secrets["GITHUB_TOKEN"]
BRANCH = "main"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Only these get "plan a gift" prominence + can theme the week. Everyone else
# is a quiet FYI.
CLOSE_FAMILY = ("esther", "henry", "taylor")

DEFAULT_OPEN_ITEMS = [
    {"id": "milo-boarding", "title": "🐶 Milo boarding — away Jul 8–19",
     "sub": "Pet Grand Hotel unavailable. Confirm Giving Tree K9 does overnight (571-799-8100).", "done": False},
    {"id": "camp-w5", "title": "🏕️ Henry camp — Week 5 (Jul 20–24)",
     "sub": "First week after the cruise. TBD.", "done": False},
    {"id": "camp-w8", "title": "🏕️ Henry camp — Week 8 (Aug 10–14)",
     "sub": "Boys back from Germany Aug 9; Esther in Sofia till Aug 14. TBD.", "done": False},
    {"id": "camp-w9", "title": "🏕️ Henry camp — Week 9 (Aug 17–21)",
     "sub": "Bridge week before Code Ninjas. TBD.", "done": False},
    {"id": "thrills-reg", "title": "📝 Summertime Thrills registration (Week 6)",
     "sub": "Register via Arlington Rec — vaarlingtonweb.myvscloud.com", "done": False},
]

# Known trips. Each carries a real end date so past trips drop off the planner
# automatically, and an optional agent_id binding it to a flight-watch agent in
# agents.json — that agent's fares then render inside the trip itself.
TRIPS = [
    {"title": "🛳️ Seattle + Alaska cruise", "window": "Jul 8–19", "end": "2026-07-19",
     "detail": "Cruise Jul 11–18 · away (Seattle) Jul 8–19 · Milo boarding + mail hold"},
    {"title": "🇩🇪 Germany", "window": "Jul 31 – Aug 9", "end": "2026-08-09",
     "detail": "Depart Jul 31 (overnight) · boys home Sun Aug 9"},
    {"title": "🇧🇬 Bulgaria — Sofia (work)", "window": "Aug 9 – 14", "end": "2026-08-14",
     "detail": "Esther · depart ~Aug 9 as boys return · back Aug 14"},
    {"title": "🇮🇹 Italy", "window": "Dec 19 – Jan 3", "end": "2027-01-03",
     "detail": "IAD → Rome Dec 19 · home Munich → IAD · comparing Jan 2 vs Jan 3 return",
     "agent_id": "italy-flights"},
]

# Agents framework — none active yet; the Italy example shows the shape.
AGENTS = []

# Recurring maintenance / health cadence — reference list for now.
STANDING_ITEMS = [
    {"name": "🌡️ HVAC service", "cadence": "every 6 months"},
    {"name": "🛢️ Car oil change", "cadence": "~every 4 months"},
    {"name": "🚗 State inspection", "cadence": "yearly"},
    {"name": "🦷 Dental cleaning (each)", "cadence": "every 6 months"},
    {"name": "🍂 Gutters", "cadence": "every 6 months"},
    {"name": "🔋 Smoke-detector batteries", "cadence": "yearly"},
    {"name": "🛂 Passports valid?", "cadence": "check before Germany / Italy"},
]


# ─────────────────────────── github i/o ───────────────────────────


def fetch_file(path: str):
    r = requests.get(f"{API_BASE}/{path}", headers=HEADERS, params={"ref": BRANCH}, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return json.loads(base64.b64decode(data["content"]).decode("utf-8")), data["sha"]


def write_file(path: str, content_obj, sha, message: str) -> None:
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(content_obj, indent=2).encode()).decode(),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    requests.put(f"{API_BASE}/{path}", headers=HEADERS, json=body, timeout=15).raise_for_status()


def log_activity(action: str, title: str, source: str = "") -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": action,
             "title": title[:120], "source": source}
    try:
        log, sha = fetch_file("activity_log.json")
        log = (log or []) + [entry]
        write_file("activity_log.json", log[-500:], sha, f"app: log {action}")
    except Exception:
        pass


def clear_item(item, pending):
    synced, ssha = fetch_file("synced_events.json")
    synced = synced or []
    if item["id"] not in synced:
        synced.append(item["id"]); synced.sort()
    write_file("synced_events.json", synced, ssha, f"app: {item['id'][:8]}")
    _, psha = fetch_file("pending_events.json")
    write_file("pending_events.json", [p for p in pending if p["id"] != item["id"]],
               psha, f"app: clear {item.get('title','')[:30]}")


def approve_item(item, pending):
    """Queue an item to be added to the calendar. The sync's actioning step
    (resolve address → round-trip drives → 9-5 work rule → create) will pick
    these up. Stored so nothing is lost before that step is wired."""
    approved, asha = fetch_file("approved_events.json")
    approved = approved or []
    if not any(a.get("id") == item["id"] for a in approved):
        approved.append(item)
    write_file("approved_events.json", approved, asha, f"app: approve {item.get('title','')[:30]}")
    clear_item(item, pending)


def _conf_sig(c) -> str:
    return f"{c.get('date')}|{c.get('a')}|{c.get('a_time')}|{c.get('b')}|{c.get('b_time')}"


def ignore_conflict(c):
    obj, sha = fetch_file("ignored_conflicts.json")
    obj = obj or []
    sig = _conf_sig(c)
    if sig not in obj:
        obj.append(sig)
    write_file("ignored_conflicts.json", obj, sha, "app: ignore conflict")


def queue_conflict_delete(date, title, c):
    q, qsha = fetch_file("conflict_deletes.json")
    q = q or []
    q.append({"date": date, "title": title})
    write_file("conflict_deletes.json", q, qsha, f"app: delete {str(title)[:24]}")
    ignore_conflict(c)  # hide from view immediately; resolver removes from calendar


# ─────────────────────────── insights engine ───────────────────────────


def _parse(iso):
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def _is_close_family(title: str) -> bool:
    t = (title or "").lower()
    return any(n in t for n in CLOSE_FAMILY)


def _llm_insights(day: dict):
    """Hook for a future LLM upgrade. Return list of (icon, text, sev) or None."""
    return None


def day_insights(day: dict):
    out = []
    events = day.get("events", [])
    timed = [e for e in events if not e.get("all_day") and not e.get("is_drive")]
    drives = [e for e in events if e.get("is_drive")]

    for c in day.get("conflicts", []):
        out.append(("⚠️", f"Conflict: {c['a']} vs {c['b']}", "warn"))

    n = len(timed)
    # Status/count tags ("N things on", "Open day — nothing scheduled") removed —
    # not additive; the day card itself already shows what's on the day.

    starts = sorted([(_parse(e["sort"]), e) for e in timed if _parse(e.get("sort", ""))],
                    key=lambda x: x[0])
    for i in range(len(starts) - 1):
        gap = (starts[i + 1][0] - starts[i][0]).total_seconds() / 60
        if 0 < gap <= 60:
            out.append(("⏱", f"Back-to-back: {starts[i][1]['title'][:22]} → "
                             f"{starts[i+1][1]['title'][:22]} ({int(gap)}m)", "warn"))
            break

    if starts:
        if starts[0][0].hour < 8:
            out.append(("⏰", f"Early start — {starts[0][1]['time']}", "info"))
        if n >= 2 and starts[-1][0].hour < 17:
            out.append(("🌙", "Evening's free", "ok"))

    if len(drives) >= 3:
        out.append(("🚗", f"{len(drives)} drives — lots of running around", "info"))

    dw = day.get("weather") or {}
    if dw:
        if dw.get("precip_pct", 0) >= 50:
            out.append(("🌧", "Rain likely — plan indoor / umbrella", "info"))
        if dw.get("high_f", 0) >= 88:
            out.append(("🥵", f"Hot ({dw.get('high_f')}°) — hydrate", "info"))

    return out


def lookahead_insights(week, horizon, open_items):
    out = []
    bdays = horizon.get("birthdays", [])
    close = [b for b in bdays if _is_close_family(b["title"])]
    others = [b for b in bdays if not _is_close_family(b["title"])]

    for b in close:
        when = "today" if b["days_out"] == 0 else f"in {b['days_out']}d"
        out.append(("🎂", f"{b['title']} — {b['day']} ({when}). Plan a gift.", "info"))
    for h in horizon.get("holidays", []):
        out.append(("🎉", f"{h['title']} — {h['day']}", "ok"))

    busiest, best = None, 0
    for d in week[1:]:
        cnt = len([e for e in d.get("events", []) if not e.get("all_day") and not e.get("is_drive")])
        if cnt > best:
            best, busiest = cnt, d
    if busiest and best >= 3:
        out.append(("🔭", f"Busiest day ahead: {busiest['label']} ({best} things)", "warn"))

    conflict_days = [d["label"] for d in week if d.get("conflicts")]
    if conflict_days:
        out.append(("⚠️", f"Conflicts to resolve: {', '.join(conflict_days[:3])}", "warn"))

    open_todo = [it for it in open_items if not it.get("done")]
    if open_todo:
        out.append(("📌", f"{len(open_todo)} open to-do{'s' if len(open_todo) != 1 else ''} "
                         f"(e.g. {open_todo[0]['title'].split('—')[0].strip()})", "info"))

    # Acquaintance birthdays — good to know, not to orient around.
    if others:
        names = ", ".join(
            f"{b['title'].replace('Birthday','').replace('’s','').replace(chr(39)+'s','').strip()} "
            f"({b['day'].split(',')[-1].strip() if ',' in b['day'] else b['day']})"
            for b in others)
        out.append(("🗒️", f"Also noting: {names}", "muted"))
    return out


def insights_for(day):
    return _llm_insights(day) or day_insights(day)


def _chips(insights) -> str:
    if not insights:
        return ""
    html = "<div class='chips'>"
    for icon, text, sev in insights:
        cls = sev if sev in ("ok", "info", "warn") else "info"
        html += f"<span class='chip chip-{cls}'>{icon} {text}</span>"
    return html + "</div>"


# ─────────────────────────── page setup ───────────────────────────

st.set_page_config(page_title="calendarjam", page_icon="📅", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
  :root { color-scheme: light; }
  .block-container { padding-top: 1.4rem; padding-bottom: 2.5rem; max-width: 1680px; }
  /* Hide Streamlit Cloud chrome (top toolbar / Fork / GitHub / menu) so it
     doesn't overlap the app's own header. */
  header[data-testid="stHeader"] { display:none !important; }
  [data-testid="stToolbar"], [data-testid="stDecoration"] { display:none !important; }
  #MainMenu, footer { display:none !important; }
  .stAppDeployButton { display:none !important; }
  /* Top-row columns: shrink to content, but cap height and scroll internally
     when very full so the week-ahead view below stays on screen. */
  .scrollcol { max-height:340px; overflow-y:auto; padding-right:4px; }
  div[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .cap-needs) {
      max-height:340px; overflow-y:auto; padding-right:5px;
  }
  .cap-needs { display:block; height:0; margin:0; padding:0; }
  .sec { font-size:11px; font-weight:800; letter-spacing:.07em; text-transform:uppercase;
         color:#9398a8; margin:13px 0 5px; }
  .card { background:#fff; border:1px solid #ececf2; border-radius:13px; padding:11px 14px;
          box-shadow:0 1px 3px rgba(20,20,40,.05); margin-bottom:8px; }
  div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:12px; }
  div[data-testid="stExpander"] details { border-radius:12px; }
  .today-card { border:1.5px solid #1a1a2e; }
  .day-head { font-weight:750; color:#1a1a2e; font-size:14px; margin-bottom:6px; line-height:1.7; }
  .ev-row { display:flex; gap:10px; padding:4px 0; border-top:1px solid #f4f4f7; }
  .ev-row.first { border-top:none; }
  .ev-time { width:68px; flex-shrink:0; color:#5b5b6b; font-weight:600;
             font-family:ui-monospace,monospace; font-size:12px; }
  .ev-title { color:#222; font-size:13.5px; line-height:1.35; }
  .ev-loc { color:#a6a6b2; font-size:11px; }
  .ev-link, .pill-link { color:inherit; text-decoration:none; }
  .ev-link:hover { text-decoration:underline; text-decoration-color:#b8b8c4; }
  .pill-link:hover { text-decoration:underline; }
  .empty-day { color:#c4c4cf; font-size:12.5px; font-style:italic; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .chip { font-size:11px; font-weight:600; padding:3px 9px; border-radius:20px; line-height:1.35; }
  .chip-ok   { background:#e9f7ef; color:#1c7a46; }
  .chip-info { background:#eef2fb; color:#34508f; }
  .chip-warn { background:#fdeee8; color:#b4471f; }
  .chip-muted{ background:#f1f1f4; color:#73737f; }
  .attention { background:linear-gradient(135deg,#fff4f0,#ffe9e0); border:1px solid #ffd9c9;
               border-radius:14px; padding:11px 15px; margin-bottom:6px; font-size:13px; color:#8a3a1a; }
  /* Week ahead: columns separated by a light vertical line (no boxes), tight
     spacing so text gets the width. */
  .week-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:0;
               align-items:stretch; }
  .wk-card { background:none; border:none; border-left:1px solid #ececf2; border-radius:0;
             box-shadow:none; padding:2px 12px; }
  .week-grid > .wk-card:first-child { border-left:none; padding-left:0; }
  @media (max-width:760px){
    /* Stacked on mobile -> horizontal dividers instead of vertical */
    .week-grid { grid-template-columns:1fr; }
    .wk-card { border-left:none; border-top:1px solid #ececf2; padding:10px 0 4px; }
    .week-grid > .wk-card:first-child { border-top:none; padding-top:2px; }
  }
  @media (min-width:761px){
    .week-grid { grid-template-columns:repeat(auto-fit, minmax(156px, 1fr)); }
    .wk-card { padding:2px 12px; }
    .wk-card .day-head { font-size:13px; margin-bottom:5px; line-height:1.5; }
    .wk-card .ev-row { padding:3px 0; }
    .wk-card .ev-time { width:56px; font-size:11px; }
    .wk-card .ev-title { font-size:13px; line-height:1.32; }
    .wk-card .chip-allday { font-size:10.5px; padding:1px 8px; }
    .wk-card .chips { margin-top:7px; }
  }
  .look-row { padding:7px 0; font-size:13.5px; color:#333; border-top:1px solid #f4f4f7; line-height:1.4; }
  .look-row.first { border-top:none; }
  .look-row.muted { color:#9a9aa7; font-size:12.5px; }
  .trip-row { padding:8px 0; border-top:1px solid #f4f4f7; }
  .trip-row.first { border-top:none; }
  .trip-title { font-size:13.5px; font-weight:600; color:#1a1a2e; }
  .trip-detail { font-size:11.5px; color:#9a9aa7; margin-top:2px; }
  .todo-sub { font-size:11px; margin:0 0 8px 28px; color:#9a9aa7; }
  .muted { color:#a6a6b2; font-size:11.5px; }
  div[data-testid="stCheckbox"] { margin-bottom:0; }

  /* All-day event pills + look-ahead strip */
  .allday-inline { margin-left:6px; }
  .allday-inline .chip-allday { margin-left:3px; vertical-align:middle; }
  .chip-allday { font-size:11px; font-weight:600; padding:2px 9px; border-radius:20px;
                 line-height:1.45; }
  .ad-bday    { background:#fce7f0; color:#a4285f; }  /* birthdays */
  .ad-travel  { background:#e3f0fb; color:#1f5fa8; }  /* trips / flights / hotels */
  .ad-house   { background:#fbefd8; color:#8a5a12; }  /* Edgar / house / chores */
  .ad-deliver { background:#e9f7ef; color:#1c7a46; }  /* deliveries */
  .ad-school  { background:#efe9fb; color:#5a34a8; }  /* school */
  .ad-default { background:#eef2fb; color:#34508f; }  /* everything else */

  /* Flight price-drop alert banner */
  .falert { background:linear-gradient(135deg,#e9f7ef,#f4fbf6); border:1.5px solid #1c7a46;
            border-radius:14px; padding:11px 15px; margin:2px 0 10px; }
  .falert-head { font-size:13px; font-weight:800; color:#14663a; letter-spacing:.02em;
                 display:flex; align-items:center; gap:7px; margin-bottom:5px; }
  .falert-row { font-size:13px; color:#1a1a2e; padding:3px 0; line-height:1.4; }
  .falert-drop { color:#1c7a46; font-weight:800; }
  .falert-badge { background:#1c7a46; color:#fff; font-size:9.5px; font-weight:700; padding:2px 7px;
                  border-radius:10px; text-transform:uppercase; letter-spacing:.04em; margin-left:6px;
                  vertical-align:middle; }
  .falert-sub { font-size:11px; color:#6f8a79; margin-top:4px; }

  /* Flight-watch cards */
  .fgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(225px,1fr)); gap:12px; margin:6px 0; }
  .fcard { background:#fff; border:1px solid #ececf2; border-radius:14px; padding:13px 15px;
           box-shadow:0 1px 3px rgba(20,20,40,.04); }
  .fcard.fbest { border:2px solid #1c7a46; }
  .fhead { font-size:13px; font-weight:700; color:#1a1a2e; display:flex; justify-content:space-between;
           align-items:center; gap:6px; }
  .fbadge { background:#e9f7ef; color:#1c7a46; font-size:9.5px; font-weight:700; padding:2px 7px;
            border-radius:10px; text-transform:uppercase; letter-spacing:.04em; white-space:nowrap; }
  .fprice { font-size:23px; font-weight:800; color:#1a1a2e; margin-top:7px; line-height:1.1; }
  .fcab { font-size:12px; font-weight:500; color:#9a9aa7; }
  .fpp { font-size:11px; color:#9a9aa7; }
  .fbiz { font-size:12px; color:#5b5b6b; margin-top:4px; }
  .fair { font-size:12.5px; color:#333; margin-top:7px; }
  .ffn { font-size:11px; color:#b0b0ba; font-family:ui-monospace,monospace; margin-top:2px; }
  .frng { font-size:11px; color:#9a9aa7; margin-top:7px; padding-top:6px; border-top:1px solid #f4f4f7; }

  /* Flight price table */
  .ftbl { width:100%; border-collapse:collapse; font-size:13px; margin-top:4px; }
  .ftbl th { text-align:left; font-size:10.5px; text-transform:uppercase; letter-spacing:.04em;
             color:#9398a8; font-weight:700; padding:6px 10px; border-bottom:1px solid #ececf2; }
  .ftbl td { padding:7px 10px; border-bottom:1px solid #f4f4f7; color:#1a1a2e; }
  .ftbl-best td { background:#dff2e6; font-weight:600; }
  .ftbl-b0 td { background:#ffffff; }
  .ftbl-b1 td { background:#f3f4f8; }
  .ffnum { font-family:ui-monospace,monospace; font-size:11.5px; color:#5b5b6b; white-space:nowrap; }

  /* Shopping deals cell — only rendered when something's on sale */
  .deal-card { background:linear-gradient(135deg,#e9f7ef,#f4fbf6); border:1.5px solid #1c7a46; }
  .deal-row { padding:8px 0; border-top:1px solid #d7ecdf; }
  .deal-row.first { border-top:none; }
  .deal-name { font-size:13.5px; font-weight:600; color:#1a1a2e; }
  .deal-price { font-size:15px; font-weight:800; color:#1c7a46; margin-top:2px; }
  .deal-was { font-size:12px; font-weight:500; color:#9a9aa7; text-decoration:line-through; margin-left:4px; }
  .deal-badge { background:#1c7a46; color:#fff; font-size:10px; font-weight:700; padding:2px 7px;
                border-radius:10px; margin-left:6px; vertical-align:middle; }
  .deal-link { font-size:12px; font-weight:700; color:#1c7a46; text-decoration:none; margin-top:3px;
               display:inline-block; }
</style>
""", unsafe_allow_html=True)

dash, _ = fetch_file("dashboard.json")
pending, _ = fetch_file("pending_events.json")
activity_log, _ = fetch_file("activity_log.json")
open_items, open_sha = fetch_file("app_open_items.json")
agents_data, _ = fetch_file("agents.json")
ignored_conflicts, _ = fetch_file("ignored_conflicts.json")
shopping, _ = fetch_file("shopping.json")
pending = pending or []
activity_log = activity_log or []
if open_items is None:
    open_items = DEFAULT_OPEN_ITEMS

theme = (dash or {}).get("theme") or {"emoji": "📅", "title": "", "blurb": "",
                                      "color1": "#1a1a2e", "color2": "#3a3a55"}
w = (dash or {}).get("weather") or {}
week = (dash or {}).get("week", [])

# Permanent ignores: title-substring patterns that must never show (work meetings
# etc. that don't belong on the personal calendar). Filter them out of every day
# before counts/conflicts/insights are computed.
pignore, _ = fetch_file("permanent_ignores.json")
_ignore_pats = [p.lower() for p in (pignore or {}).get("title_patterns", []) if p]
if _ignore_pats:
    for _d in week:
        _d["events"] = [e for e in _d.get("events", [])
                        if not any(p in (e.get("title") or "").lower() for p in _ignore_pats)]
    pending = [p for p in pending
               if not any(q in (p.get("title") or "").lower() for q in _ignore_pats)]
horizon = (dash or {}).get("horizon", {})

import re as _re


def _norm_title(t: str) -> str:
    t = (t or "").lower()
    t = _re.sub(r"^(your|re:|fw:|fwd:|reminder:)\s+", "", t)
    t = _re.sub(r"[^a-z0-9 ]", " ", t)
    return _re.sub(r"\s+", " ", t).strip()


# Deterministic dedup guard: a review suggestion whose title matches an event
# already on the calendar (this week's snapshot) is dropped — it's not a decision
# she needs to make. Belt-and-suspenders on top of the morning-pass dedup rule.
_cal_titles = []
for _d in week:
    for _e in _d.get("events", []):
        _nt = _norm_title(_e.get("title"))
        if len(_nt) >= 8:
            _cal_titles.append(_nt)


def _already_on_calendar(item: dict) -> bool:
    pt = _norm_title(item.get("title"))
    if len(pt) < 8:
        return False
    return any(ct in pt or pt in ct for ct in _cal_titles)


_pending_before = len(pending)
pending = [it for it in pending if not _already_on_calendar(it)]
_pending_deduped = _pending_before - len(pending)

ignored_set = set(ignored_conflicts or [])
all_conflicts = []
for day in week:
    for c in day.get("conflicts", []):
        cc = {**c, "day": day["label"], "date": day.get("date")}
        if _conf_sig(cc) not in ignored_set:
            all_conflicts.append(cc)


def _allday_class(title: str) -> str:
    """Color-code all-day pills by category. Keyword buckets, expanded over time;
    no legend — the colors get learned by pattern."""
    t = (title or "").lower()
    def has(*w): return any(x in t for x in w)
    if "🎂" in title or has("birthday", "bday", "b-day"):
        return "ad-bday"
    if (has("cruise", "flight", "stay at", "hotel", "trip", "airport", "vacation",
            "resort", "airbnb", "getaway", "flying", "depart", "layover",
            "check-in", "check out", "boarding")
            or any(e in title for e in ("✈", "🛳", "🛫", "🏝", "🚢"))):
        return "ad-travel"
    if has("edgar", "washer", "lint", "dishwasher", "filter", "trash", "lawn",
           "clean", "hvac", "plumb", "repair", "handyman", "chore", "gutter",
           "furnace", "yard", "mow", "vacuum", "house"):
        return "ad-house"
    if (has("delivery", "mightymeals", "walmart", "grocery", "package", "arrives")
            or any(e in title for e in ("🍱", "🛒", "📦"))):
        return "ad-deliver"
    if "📚" in title or has("school", "holiday", "early release", "sol",
                                    "no school", "break", "swanson"):
        return "ad-school"
    return "ad-default"


_DOW_SHORT = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
              "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun"}


def _short_label(label: str) -> str:
    """Abbreviate full weekday names (Monday -> Mon) in a day label."""
    for full, ab in _DOW_SHORT.items():
        label = label.replace(full, ab)
    return label


def _day_head(day: dict) -> str:
    """Date header with color-coded all-day pills inline (next to the date, so
    they don't cost an extra row)."""
    allday = [e for e in day.get("events", []) if e.get("all_day")]
    pills = ""
    for e in allday:
        label = e["title"]
        if e.get("link"):
            label = (f"<a class='pill-link' href='{e['link']}' target='_blank' "
                     f"rel='noopener'>{e['title']}</a>")
        pills += f"<span class='chip-allday {_allday_class(e['title'])}'>{label}</span>"
    inline = f"<span class='allday-inline'>{pills}</span>" if pills else ""
    return f"<div class='day-head'>{_short_label(day['label'])}{inline}</div>"


def _day_card_inner(day: dict) -> str:
    evs = day.get("events", [])
    if not evs:
        return "<div class='empty-day'>nothing scheduled</div>"
    timed = [e for e in evs if not e.get("all_day")]
    rows = ""
    for i, e in enumerate(timed):
        loc = f"<div class='ev-loc'>📍 {e['location'][:40]}</div>" if e.get("location") else ""
        first = " first" if i == 0 else ""
        title = e["title"]
        if e.get("link"):
            title = (f"<a class='ev-link' href='{e['link']}' target='_blank' "
                     f"rel='noopener'>{e['title']}</a>")
        rows += (f"<div class='ev-row{first}'><div class='ev-time'>{e['time']}</div>"
                 f"<div><div class='ev-title'>{title}</div>{loc}</div></div>")
    if not timed:
        rows = "<div class='empty-day' style='padding-top:1px'>no timed plans</div>"
    return rows


def flight_price_alerts(agents_data: dict) -> list:
    """From each flight agent's history, surface meaningful price drops and new
    lows (latest snapshot vs the run before it, and vs the full tracked range).
    Tiny $1-2 wiggles are ignored; a new all-time low always surfaces."""
    NOISE = 25  # ignore sub-$25 day-to-day jitter for plain drops
    out = []
    for ag in (agents_data or {}).get("agents", []):
        if ag.get("type") != "flight-multicity":
            continue
        hist = ag.get("history", [])
        if len(hist) < 2:
            continue
        latest, prev = hist[-1], hist[-2]
        pax = ag.get("config", {}).get("travelers", 1)
        for key, val in latest.items():
            if key == "date" or not isinstance(val, (int, float)):
                continue
            prior = [h[key] for h in hist[:-1]
                     if isinstance(h.get(key), (int, float))]
            if not prior:
                continue
            prev_val = prev.get(key)
            prior_low = min(prior)
            if val < prior_low:  # new all-time low over the tracked window
                out.append({"label": key, "val": val, "drop": prior_low - val,
                            "prev": prev_val, "pax": pax, "kind": "low"})
            elif isinstance(prev_val, (int, float)) and prev_val - val >= NOISE:
                out.append({"label": key, "val": val, "drop": prev_val - val,
                            "prev": prev_val, "pax": pax, "kind": "drop"})
    out.sort(key=lambda a: a["drop"], reverse=True)
    return out


# ─────────────────────────── header ───────────────────────────

st.markdown(
    f"<div style='height:4px;border-radius:4px;margin-bottom:6px;"
    f"background:linear-gradient(90deg,{theme['color1']},{theme['color2']})'></div>",
    unsafe_allow_html=True,
)
wx = (f"<span class='muted'>&nbsp;&nbsp;{w.get('emoji','')} {w.get('high_f','–')}°/{w.get('low_f','–')}°"
      f"·{w.get('precip_pct',0)}%</span>") if w else ""
chip = (f"<span style='margin-left:9px;background:linear-gradient(135deg,{theme['color1']},{theme['color2']});"
        f"color:#fff;font-size:11px;font-weight:700;padding:3px 10px;border-radius:13px'>"
        f"{theme['emoji']} {theme['title']}</span>") if theme.get("title") else ""
blurb = (f"<span class='muted' style='margin-left:10px;font-size:12px'>{theme['blurb']}</span>"
         if theme.get("blurb") else "")
st.markdown(
    f"<div style='display:flex;align-items:center;flex-wrap:wrap'>"
    f"<span style='font-size:20px;font-weight:800;color:#1a1a2e'>📅 calendarjam</span>"
    f"{chip}{blurb}{wx}</div>",
    unsafe_allow_html=True,
)

if not dash:
    st.warning("No dashboard snapshot yet — the next 6 AM sync will populate this.", icon="⏳")

# ─────────────────────────── flight price-drop alerts (top of page) ───────────────────────────
_fal = flight_price_alerts(agents_data)
if _fal:
    _rows = ""
    for _a in _fal[:6]:
        _pax = _a["pax"] or 1
        _badge = "<span class='falert-badge'>new low</span>" if _a["kind"] == "low" else ""
        _rows += (f"<div class='falert-row'>{_a['label']} — "
                  f"<span class='falert-drop'>▼ ${_a['drop']:,.0f}</span> to "
                  f"<b>${_a['val']:,.0f}</b> "
                  f"<span class='muted'>(≈${_a['val']/_pax:,.0f}/person)</span>{_badge}</div>")
    st.markdown(
        f"<div class='falert'><div class='falert-head'>✈️ Fare drop</div>{_rows}"
        f"<div class='falert-sub'>Total price · vs the previous check · "
        f"full tracker under Planning ↓</div></div>",
        unsafe_allow_html=True,
    )

# ─────────────────────────── shopping deals (always visible) ───────────────────────────

# Sale-watch list. The panel is a standing list so it's obvious what's being
# watched even on days when nothing is discounted.
WATCHED_VENDORS = ("Bombas", "OOFOS", "Cotopaxi")


def _vendor_of(p: dict) -> str:
    blob = f"{p.get('id','')} {p.get('url','')} {p.get('name','')}".lower()
    for v in WATCHED_VENDORS:
        if v.lower() in blob:
            return v
    return (p.get("name") or "Tracked").split()[0]


_products = (shopping or {}).get("products", [])
_by_vendor = {v: [] for v in WATCHED_VENDORS}
for _p in _products:
    _by_vendor.setdefault(_vendor_of(_p), []).append(_p)
_any_deal = any(p.get("status", {}).get("is_deal") for p in _products)

rows = ""
for i, (_vendor, _items) in enumerate(_by_vendor.items()):
    first = " first" if i == 0 else ""
    if not _items:
        rows += (f"<div class='deal-row{first}'><div class='deal-name'>{_vendor}</div>"
                 f"<span class='muted'>watching · nothing tracked yet</span></div>")
        continue
    for p in _items:
        s = p.get("status", {})
        pct = s.get("pct_off", 0)
        if s.get("is_deal"):
            badge = f"<span class='deal-badge'>&minus;{pct}%</span>" if pct else ""
            was = (f"<span class='deal-was'>was ${s['msrp']:,.2f}</span>"
                   if s.get("msrp") and s.get("price") and s["price"] < s["msrp"] else "")
            stock = "in stock" if s.get("in_stock") else "⚠️ out of stock"
            rows += (f"<div class='deal-row{first}'>"
                     f"<div class='deal-name'>{_vendor} · {p.get('name','')}</div>"
                     f"<div class='deal-price'>${s.get('price',0):,.2f}{was}{badge}</div>"
                     f"<a class='deal-link' href='{p.get('buy_url','#')}' target='_blank'>View &rarr;</a>"
                     f"<span class='muted'> · {stock}</span></div>")
        else:
            rows += (f"<div class='deal-row{first}'>"
                     f"<div class='deal-name'>{_vendor} · {p.get('name','')}</div>"
                     f"<span class='muted'>${s.get('price',0):,.2f} · no markdown "
                     f"(low ${s.get('msrp',0):,.2f})</span></div>")

_deal_head = "🛍️ Deals — on sale now" if _any_deal else "🛍️ Deals worth a look"
_deal_cls = "card deal-card" if _any_deal else "card"
st.markdown(f"<div class='{_deal_cls}'><div class='day-head'>{_deal_head}</div>{rows}</div>",
            unsafe_allow_html=True)

# ─────────────────────────── 2. today (+ insights) ───────────────────────────

c1, c2 = st.columns(2, gap="medium")

with c1:
    st.markdown("<div class='sec'>📌 Today</div>", unsafe_allow_html=True)
    if week:
        st.markdown("<div class='scrollcol'>"
                    f"{_day_head(week[0])}"
                    f"{_day_card_inner(week[0])}{_chips(insights_for(week[0]))}"
                    "</div>", unsafe_allow_html=True)
    else:
        st.info("Agenda appears after the next sync.")

with c2:
    n_needs = len(pending) + len(all_conflicts)
    st.markdown(f"<div class='sec'>✅ Needs you{f' · {n_needs}' if n_needs else ''}</div>",
                unsafe_allow_html=True)
    with st.container():
        st.markdown("<div class='cap-needs'></div>", unsafe_allow_html=True)
        if not pending and not all_conflicts:
            st.success("All clear.")
        for ci, c in enumerate(all_conflicts):
            with st.container(border=True):
                st.markdown(f"⚠️ **Conflict — {c['day']}**")
                st.markdown(f"<div style='font-size:12px;color:#555'>{c['a']} ({c['a_time']}) "
                            f"vs {c['b']} ({c['b_time']})</div>", unsafe_allow_html=True)
                if st.button("🚫 Ignore — not a clash", key=f"cig{ci}", use_container_width=True):
                    ignore_conflict(c); st.toast("Conflict ignored", icon="🚫"); st.rerun()
                if st.button(f"🗑 Delete: {c['a'][:20]}", key=f"cda{ci}", use_container_width=True):
                    queue_conflict_delete(c.get("date"), c["a"], c)
                    st.toast("Delete queued (~15 min)", icon="🗑"); st.rerun()
                if st.button(f"🗑 Delete: {c['b'][:20]}", key=f"cdb{ci}", use_container_width=True):
                    queue_conflict_delete(c.get("date"), c["b"], c)
                    st.toast("Delete queued (~15 min)", icon="🗑"); st.rerun()
        for idx, item in enumerate(pending):
            title = item.get("title", "(untitled)")
            source = item.get("source", "")
            sender = item.get("from", "").split("<")[0].strip().strip('"') or "—"
            desc = (item.get("description") or "").replace("\r", "").strip()
            if len(desc) > 90:
                desc = desc[:90] + "…"
            with st.container(border=True):
                st.markdown(f"<div style='font-weight:600;font-size:13px;line-height:1.3'>{title}</div>"
                            f"<div style='color:#9a9aa7;font-size:11px;margin:1px 0 3px'>{source} · {sender}</div>"
                            + (f"<div style='color:#666;font-size:11.5px;line-height:1.4'>{desc}</div>" if desc else ""),
                            unsafe_allow_html=True)
                if st.button("✅ Add it", key=f"y{idx}", type="primary", use_container_width=True):
                    approve_item(item, pending); log_activity("approved", title, source)
                    st.toast("Queued to add ✓", icon="✅"); st.rerun()
                if st.button("🔁 Duplicate", key=f"d{idx}", use_container_width=True):
                    clear_item(item, pending); log_activity("duplicate", title, source)
                    st.toast("Marked duplicate", icon="🔁"); st.rerun()
                if st.button("🚫 Ignore", key=f"n{idx}", use_container_width=True):
                    clear_item(item, pending); log_activity("dismissed", title, source)
                    st.toast("Ignored", icon="🚫"); st.rerun()

# ─────────────────────────── the week ───────────────────────────

if len(week) > 1:
    st.markdown("<div class='sec'>🗓️ The week ahead</div>", unsafe_allow_html=True)
    cards = ""
    for day in week[1:]:
        cards += (f"<div class='wk-card'>{_day_head(day)}"
                  f"{_day_card_inner(day)}{_chips(insights_for(day))}</div>")
    st.markdown(f"<div class='week-grid'>{cards}</div>", unsafe_allow_html=True)

# ─────────────────────────── 6. planning (collapsible) ───────────────────────────

st.markdown("<div class='sec'>🧭 Planning</div>", unsafe_allow_html=True)

trip_rows = ""
for i, t in enumerate(TRIPS):
    first = " first" if i == 0 else ""
    trip_rows += (f"<div class='trip-row{first}'><div class='trip-title'>{t['title']} "
                  f"<span class='muted'>· {t['window']}</span></div>"
                  f"<div class='trip-detail'>{t['detail']}</div></div>")
st.markdown(f"<div class='card'><div class='day-head'>🧳 Trips</div>{trip_rows}</div>",
            unsafe_allow_html=True)

# ── Flight-watch agents (full-width, human view) ──
for ag in (agents_data or {}).get("agents", []):
    if ag.get("type") != "flight-multicity":
        continue
    stt = ag.get("status", {})
    pax = ag.get("config", {}).get("travelers", 1)
    st.markdown(f"<div class='sec' style='margin-top:16px'>{ag['name']}</div>", unsafe_allow_html=True)
    if stt.get("state") != "live":
        st.info(stt.get("note", "Not yet active."))
        continue
    cp, ce = stt.get("cheapest_plan"), stt.get("cheapest_econ")
    hist = ag.get("history", [])
    if cp and ce:
        st.markdown(
            f"<div style='font-size:14px;margin-bottom:2px'>Cheapest right now: "
            f"<b>{cp}</b> — <b>${ce:,.0f}</b> economy "
            f"<span class='muted'>(≈${ce/pax:,.0f}/person)</span></div>"
            f"<div class='muted' style='margin-bottom:8px'>Prices are total for {pax} · "
            f"{ag.get('config',{}).get('airline_label','')} · updated {stt.get('updated','—')}</div>",
            unsafe_allow_html=True)
    rows_html = ""
    for ri, res in enumerate(stt.get("results", [])):
        lab = res["label"]
        band = "ftbl-b0" if ri % 2 == 0 else "ftbl-b1"
        for cabin in ("economy", "business"):
            cd = res.get(cabin) or {}
            cur = cd.get("low")
            if cur is None:
                continue
            vals = [h.get(f"{lab} {cabin}") for h in hist if h.get(f"{lab} {cabin}")]
            lo = min(vals) if vals else cur
            hi = max(vals) if vals else cur
            flights = " / ".join(cd.get("flight_numbers") or []) or "—"
            cls = "ftbl-best" if (cabin == "economy" and lab == cp) else band
            rows_html += (f"<tr class='{cls}'><td>{lab}</td><td>{cabin.title()}</td>"
                          f"<td>${cur:,.0f}</td><td>${lo:,.0f}</td><td>${hi:,.0f}</td>"
                          f"<td class='ffnum'>{flights}</td></tr>")
    st.markdown(
        "<table class='ftbl'><thead><tr><th>Route</th><th>Cabin</th>"
        "<th>Today</th><th>Low seen</th><th>High seen</th><th>Flights</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        f"<div class='muted' style='margin-top:6px'>Prices total for {pax} travelers · "
        "Low/High = range tracked so far.</div>",
        unsafe_allow_html=True)

    if hist:
        hist_df = pd.DataFrame(hist)
        if "date" in hist_df.columns:
            hist_df = hist_df.set_index("date")
            _since = hist[0].get("date", "—")
            _n = len(hist)
            st.markdown(
                f"<div class='muted' style='margin:14px 0 2px'>📈 Price history — economy &amp; "
                f"business, per plan · tracking since {_since} · {_n} "
                f"snapshot{'s' if _n != 1 else ''}</div>",
                unsafe_allow_html=True)
            st.line_chart(hist_df, height=250)
        else:
            st.caption("📈 Price history will build here as daily checks accumulate.")
    else:
        st.caption("📈 Price history will build here as daily checks accumulate.")

with st.expander("📋 To-do · open items", expanded=False):
    changed = False
    for it in open_items:
        done = it.get("done", False)
        label = f"~~{it['title']}~~  ✅" if done else it["title"]
        new_val = st.checkbox(label, value=done, key="oi_" + it["id"])
        if it.get("sub"):
            sub_color = "#cccdd6" if (new_val or done) else "#9a9aa7"
            st.markdown(f"<div class='todo-sub' style='color:{sub_color}'>{it['sub']}</div>",
                        unsafe_allow_html=True)
        if new_val != done:
            it["done"] = new_val
            changed = True
    if changed:
        try:
            write_file("app_open_items.json", open_items, open_sha, "app: update to-do items")
            st.toast("Saved ✓", icon="✅")
        except Exception:
            st.toast("Couldn't save — try again", icon="⚠️")
        st.rerun()

with st.expander("🧰 Standing items (recurring)", expanded=False):
    st.caption("Recurring maintenance & health cadence — reference for now.")
    for s in STANDING_ITEMS:
        st.markdown(f"<div class='look-row'>{s['name']} <span class='muted'>· {s['cadence']}</span></div>",
                    unsafe_allow_html=True)

# ─────────────────────────── footer ───────────────────────────

with st.expander("📜 Recent activity"):
    if not activity_log:
        st.caption("No recent activity.")
    else:
        icons = {"added": "✓", "acked": "✓", "dismissed": "✗", "skipped": "✗"}
        for e in sorted(activity_log, key=lambda x: x.get("ts", ""), reverse=True)[:30]:
            try:
                d = datetime.fromisoformat(e["ts"]).astimezone().strftime("%b %-d")
            except Exception:
                d = "—"
            st.markdown(f"<div style='font-size:13px;color:#555;padding:1px 0'>"
                        f"<span style='color:#aaa'>{d}</span> &nbsp; "
                        f"{icons.get(e.get('action'),'·')} {e.get('title','')[:58]}</div>",
                        unsafe_allow_html=True)

st.divider()
fc1, fc2 = st.columns([3, 1])
with fc1:
    synced_txt = (f"🔄 Last synced {dash['generated_label']}" if dash and dash.get("generated_label")
                  else "🔄 Awaiting first sync")
    st.caption(f"{synced_txt} · runs 6 AM ET daily")
with fc2:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

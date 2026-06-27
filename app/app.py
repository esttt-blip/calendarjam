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

import requests
import streamlit as st

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

# Known trips — always shown. Placeholders until dates firm up; each can later
# hold its own prep checklist + attached agents.
TRIPS = [
    {"title": "🛳️ Seattle + Alaska cruise", "window": "Jul 8–19",
     "detail": "Cruise Jul 11–18 · away (Seattle) Jul 8–19 · Milo boarding + mail hold"},
    {"title": "🇩🇪 Germany", "window": "Jul 31 – Aug 9",
     "detail": "Depart Jul 31 (overnight) · boys home Sun Aug 9"},
    {"title": "🇧🇬 Bulgaria — Sofia (work)", "window": "Aug 9 – 14",
     "detail": "Esther · depart ~Aug 9 as boys return · back Aug 14"},
    {"title": "🇮🇹 Italy", "window": "Christmas", "detail": "December · flight-watch candidate"},
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
    if n == 0 and not day.get("conflicts"):
        out.append(("🟢", "Open day — nothing scheduled", "ok"))
    elif n >= 4:
        out.append(("🔴", f"Packed — {n} commitments", "warn"))
    elif n >= 1:
        out.append(("📋", f"{n} thing{'s' if n != 1 else ''} on", "info"))

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
  .block-container { padding-top: 0.8rem; padding-bottom: 2.5rem; max-width: 1240px; }
  .sec { font-size:11px; font-weight:800; letter-spacing:.07em; text-transform:uppercase;
         color:#9398a8; margin:13px 0 5px; }
  .card { background:#fff; border:1px solid #ececf2; border-radius:13px; padding:11px 14px;
          box-shadow:0 1px 3px rgba(20,20,40,.05); margin-bottom:8px; }
  div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:12px; }
  div[data-testid="stExpander"] details { border-radius:12px; }
  .today-card { border:1.5px solid #1a1a2e; }
  .day-head { font-weight:750; color:#1a1a2e; font-size:14px; margin-bottom:6px; }
  .ev-row { display:flex; gap:10px; padding:4px 0; border-top:1px solid #f4f4f7; }
  .ev-row.first { border-top:none; }
  .ev-time { width:68px; flex-shrink:0; color:#5b5b6b; font-weight:600;
             font-family:ui-monospace,monospace; font-size:12px; }
  .ev-title { color:#222; font-size:13.5px; line-height:1.35; }
  .ev-loc { color:#a6a6b2; font-size:11px; }
  .empty-day { color:#c4c4cf; font-size:12.5px; font-style:italic; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .chip { font-size:11px; font-weight:600; padding:3px 9px; border-radius:20px; line-height:1.35; }
  .chip-ok   { background:#e9f7ef; color:#1c7a46; }
  .chip-info { background:#eef2fb; color:#34508f; }
  .chip-warn { background:#fdeee8; color:#b4471f; }
  .chip-muted{ background:#f1f1f4; color:#73737f; }
  .attention { background:linear-gradient(135deg,#fff4f0,#ffe9e0); border:1px solid #ffd9c9;
               border-radius:14px; padding:11px 15px; margin-bottom:6px; font-size:13px; color:#8a3a1a; }
  .week-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:12px; }
  .wk-card { background:#fff; border:1px solid #ececf2; border-radius:14px; padding:11px 13px;
             box-shadow:0 1px 3px rgba(20,20,40,.04); }
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
</style>
""", unsafe_allow_html=True)

dash, _ = fetch_file("dashboard.json")
pending, _ = fetch_file("pending_events.json")
activity_log, _ = fetch_file("activity_log.json")
open_items, open_sha = fetch_file("app_open_items.json")
agents_data, _ = fetch_file("agents.json")
pending = pending or []
activity_log = activity_log or []
if open_items is None:
    open_items = DEFAULT_OPEN_ITEMS

theme = (dash or {}).get("theme") or {"emoji": "📅", "title": "", "blurb": "",
                                      "color1": "#1a1a2e", "color2": "#3a3a55"}
w = (dash or {}).get("weather") or {}
week = (dash or {}).get("week", [])
horizon = (dash or {}).get("horizon", {})

all_conflicts = []
for day in week:
    for c in day.get("conflicts", []):
        all_conflicts.append({**c, "day": day["label"], "date": day.get("date")})


def _day_card_inner(day: dict) -> str:
    rows = ""
    evs = day.get("events", [])
    if evs:
        for i, e in enumerate(evs):
            loc = f"<div class='ev-loc'>📍 {e['location'][:40]}</div>" if e.get("location") else ""
            first = " first" if i == 0 else ""
            rows += (f"<div class='ev-row{first}'><div class='ev-time'>{e['time']}</div>"
                     f"<div><div class='ev-title'>{e['title']}</div>{loc}</div></div>")
    else:
        rows = "<div class='empty-day'>nothing scheduled</div>"
    return rows


# ─────────────────────────── header ───────────────────────────

st.markdown(
    f"<div style='height:6px;border-radius:6px;margin-bottom:12px;"
    f"background:linear-gradient(90deg,{theme['color1']},{theme['color2']})'></div>",
    unsafe_allow_html=True,
)
wx = (f"<span class='muted'>&nbsp;&nbsp;{w.get('emoji','')} {w.get('high_f','–')}°/{w.get('low_f','–')}°"
      f"·{w.get('precip_pct',0)}%</span>") if w else ""
chip = (f"<span style='margin-left:9px;background:linear-gradient(135deg,{theme['color1']},{theme['color2']});"
        f"color:#fff;font-size:11px;font-weight:700;padding:3px 10px;border-radius:13px'>"
        f"{theme['emoji']} {theme['title']}</span>") if theme.get("title") else ""
st.markdown(
    f"<div style='display:flex;align-items:center;flex-wrap:wrap'>"
    f"<span style='font-size:24px;font-weight:800;color:#1a1a2e'>📅 calendarjam</span>{chip}{wx}</div>"
    + (f"<div class='muted' style='margin-top:2px'>{theme['blurb']}</div>" if theme.get("blurb") else ""),
    unsafe_allow_html=True,
)

if not dash:
    st.warning("No dashboard snapshot yet — the next 6 AM sync will populate this.", icon="⏳")

# ─────────────────────────── 1. attention strip ───────────────────────────

n_pending = len(pending)
if all_conflicts or n_pending:
    bits = []
    if all_conflicts:
        bits.append(f"⚠️ {len(all_conflicts)} conflict{'s' if len(all_conflicts) != 1 else ''}")
    if n_pending:
        bits.append(f"✅ {n_pending} to review")
    st.markdown(f"<div class='attention'><b>Heads up</b> &nbsp;·&nbsp; {' &nbsp;·&nbsp; '.join(bits)}</div>",
                unsafe_allow_html=True)

# ─────────────────────────── 2. today (+ insights) ───────────────────────────

LOOK = lookahead_insights(week, horizon, open_items)
CAP = 300  # column height cap — all three level; scroll within when full
c1, c2, c3 = st.columns(3, gap="medium")

with c1:
    st.markdown("<div class='sec'>📌 Today</div>", unsafe_allow_html=True)
    with st.container(height=CAP):
        if week:
            st.markdown(f"<div class='day-head'>{week[0]['label']}</div>"
                        f"{_day_card_inner(week[0])}{_chips(insights_for(week[0]))}",
                        unsafe_allow_html=True)
        else:
            st.info("Agenda appears after the next sync.")

with c2:
    n_needs = len(pending) + len(all_conflicts)
    st.markdown(f"<div class='sec'>✅ Needs you{f' · {n_needs}' if n_needs else ''}</div>",
                unsafe_allow_html=True)
    with st.container(height=CAP):
        if not pending and not all_conflicts:
            st.success("All clear.")
        for c in all_conflicts:
            with st.container(border=True):
                st.markdown(f"⚠️ **Conflict — {c['day']}**")
                st.markdown(f"<div style='font-size:12px;color:#555'>{c['a']} ({c['a_time']}) "
                            f"vs {c['b']} ({c['b_time']})</div>", unsafe_allow_html=True)
                _d = c.get("date")
                if _d and len(_d.split("-")) == 3:
                    _y, _m, _dd = _d.split("-")
                    st.link_button(
                        "Fix in Google Calendar ↗",
                        f"https://calendar.google.com/calendar/r/day/{int(_y)}/{int(_m)}/{int(_dd)}",
                        use_container_width=True)
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

with c3:
    st.markdown("<div class='sec'>🔭 Look ahead</div>", unsafe_allow_html=True)
    with st.container(height=CAP):
        if LOOK:
            rows = ""
            for i, (icon, text, sev) in enumerate(LOOK):
                first = " first" if i == 0 else ""
                muted = " muted" if sev == "muted" else ""
                rows += f"<div class='look-row{first}{muted}'>{icon} {text}</div>"
            st.markdown(rows, unsafe_allow_html=True)
        else:
            st.caption("Nothing in the next few weeks.")

# ─────────────────────────── the week ───────────────────────────

if len(week) > 1:
    st.markdown("<div class='sec'>🗓️ The week ahead</div>", unsafe_allow_html=True)
    cards = ""
    for day in week[1:]:
        cards += (f"<div class='wk-card'><div class='day-head'>{day['label']}</div>"
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
    cards = ""
    for res in stt.get("results", []):
        lab = res["label"]
        econ = res.get("economy") or {}
        biz = res.get("business") or {}
        best = (lab == cp)
        airline = (econ.get("airlines") or ["—"])[0]
        stops = econ.get("layovers", 0)
        stoptxt = "nonstop" if not stops else f"{stops} stop"
        fns = " · ".join(econ.get("flight_numbers") or [])
        vals = [h.get(f"{lab} economy") for h in hist if h.get(f"{lab} economy")]
        rngtxt = (f"tracked ${min(vals):,.0f}–${max(vals):,.0f} · {len(vals)} checks"
                  if len(vals) >= 2 else "tracking begins next check")
        econ_str = f"${econ['low']:,.0f}" if econ.get("low") else "—"
        pp = f"<span class='fpp'> · ≈${econ['low']/pax:,.0f}/person</span>" if econ.get("low") else ""
        biz_str = f"${biz['low']:,.0f}" if biz.get("low") else "—"
        badge = "<span class='fbadge'>cheapest</span>" if best else ""
        cards += (
            f"<div class='fcard{' fbest' if best else ''}'>"
            f"<div class='fhead'><span>{lab}</span>{badge}</div>"
            f"<div class='fprice'>{econ_str}<span class='fcab'> economy</span></div>"
            f"<div class='fpp'>{('≈$' + format(econ['low']/pax, ',.0f') + '/person') if econ.get('low') else ''}</div>"
            f"<div class='fbiz'>Business {biz_str}</div>"
            f"<div class='fair'>{airline} · {stoptxt}</div>"
            f"<div class='ffn'>{fns}</div>"
            f"<div class='frng'>{rngtxt}</div>"
            f"</div>")
    st.markdown(f"<div class='fgrid'>{cards}</div>", unsafe_allow_html=True)
    if cp:
        series = [h.get(f"{cp} economy") for h in hist if h.get(f"{cp} economy")]
        if len(series) >= 2:
            st.caption(f"{cp} — economy price trend")
            st.line_chart(series, height=120)

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

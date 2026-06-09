"""calendarjam — command station.

A holistic agenda: decisions to make (review queue) up top, then the full
week laid out day-by-day with events, deliveries, conflicts inline, and
upcoming birthdays/holidays. Reads from the GitHub repo:
  - dashboard.json      (week agenda + horizon, written by sync)
  - pending_events.json (review queue — interactive)
  - activity_log.json   (recent decisions)

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
    """Shared handler for Got it / Skip — removes from pending, marks synced, logs."""
    synced, ssha = fetch_file("synced_events.json")
    synced = synced or []
    if item["id"] not in synced:
        synced.append(item["id"]); synced.sort()
    write_file("synced_events.json", synced, ssha, f"app: {item['id'][:8]}")
    _, psha = fetch_file("pending_events.json")
    write_file("pending_events.json", [p for p in pending if p["id"] != item["id"]],
               psha, f"app: clear {item.get('title','')[:30]}")


# ─────────────────────────── page setup ───────────────────────────

st.set_page_config(page_title="calendarjam", page_icon="📅", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1180px; }
  /* Day cards sit in a grid; fixed-ish height keeps the grid tidy */
  .day-card { background:#fff; border:1px solid #e6e6ec; border-radius:12px;
              padding:12px 16px; margin-bottom:14px; height:230px; overflow-y:auto; }
  .day-card.today { border:2px solid #1a1a2e; height:auto; }
  .day-head { font-weight:700; color:#1a1a2e; font-size:14px; margin-bottom:8px;
              padding-bottom:4px; border-bottom:1px solid rgba(0,0,0,.06); }
  .ev-row { border-bottom:1px solid rgba(0,0,0,.04); }
  .ev-row { display:flex; padding:5px 0; border-bottom:1px solid #f6f6f6; }
  .ev-time { width:74px; flex-shrink:0; color:#1a1a2e; font-weight:600;
             font-family:ui-monospace,monospace; font-size:12.5px; }
  .ev-body { flex:1; }
  .ev-title { color:#222; font-size:13.5px; line-height:1.3; }
  .ev-loc { color:#999; font-size:11.5px; }
  .conflict { background:#fff4f0; border-left:3px solid #ff6b35; border-radius:6px;
              padding:7px 10px; margin:8px 0; font-size:12.5px; color:#8a3a1a; }
  .empty-day { color:#c4c4c4; font-size:13px; font-style:italic; }
</style>
""", unsafe_allow_html=True)

dash, _ = fetch_file("dashboard.json")
pending, _ = fetch_file("pending_events.json")
activity_log, _ = fetch_file("activity_log.json")
pending = pending or []
activity_log = activity_log or []

# ─────────────────────────── header (theme-integrated) ───────────────────────

APP_URL = st.secrets.get("APP_URL", "https://calendarjam-ees.streamlit.app")
theme = (dash or {}).get("theme") or {"emoji": "📅", "title": "", "blurb": "",
                                      "color1": "#1a1a2e", "color2": "#3a3a55"}
w = (dash or {}).get("weather") or {}

# Thin gradient accent bar carries the week's theme colors across the top
st.markdown(
    f"<div style='height:6px;border-radius:6px;margin-bottom:14px;"
    f"background:linear-gradient(90deg,{theme['color1']},{theme['color2']})'></div>",
    unsafe_allow_html=True,
)

hc1, hc2 = st.columns([3, 2])
with hc1:
    chip = (f"<a href='{APP_URL}' target='_self' style='text-decoration:none;"
            f"display:inline-block;margin-left:10px;vertical-align:middle;"
            f"background:linear-gradient(135deg,{theme['color1']},{theme['color2']});"
            f"color:#fff;font-size:12px;font-weight:700;padding:4px 11px;border-radius:14px'>"
            f"{theme['emoji']} {theme['title']}</a>") if theme.get("title") else ""
    st.markdown(
        f"<div style='display:flex;align-items:center'>"
        f"<a href='{APP_URL}' target='_self' style='text-decoration:none;color:#1a1a2e'>"
        f"<span style='font-size:26px;font-weight:800'>📅 calendarjam</span></a>{chip}</div>"
        + (f"<div style='color:#888;font-size:12.5px;margin-top:2px'>{theme['blurb']}</div>"
           if theme.get("blurb") else ""),
        unsafe_allow_html=True,
    )
with hc2:
    if w:
        st.markdown(
            f"<div style='text-align:right;padding-top:6px'>"
            f"<span style='font-size:24px'>{w.get('emoji','')}</span> "
            f"<span style='font-size:14px;color:#555'>{w.get('high_f','–')}°/{w.get('low_f','–')}°"
            f" · {w.get('precip_pct',0)}%</span></div>",
            unsafe_allow_html=True,
        )

st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

if not dash:
    st.warning("No dashboard snapshot yet — the next 6:15 AM sync will populate this.", icon="⏳")

week = (dash or {}).get("week", [])
all_conflicts = []
for day in week:
    for c in day.get("conflicts", []):
        all_conflicts.append({**c, "day": day["label"]})


def _weather_bg(w: dict | None) -> str:
    """Soft background tint for a day card based on its weather —
    color *is* the forecast: yellow=sun, blue=rain, red/yellow=hot, grey=cloud."""
    if not w:
        return "#ffffff"
    code = w.get("weather_code", 0)
    precip = w.get("precip_pct", 0)
    high = w.get("high_f", 0)
    if high >= 88:                                   # hot
        return "linear-gradient(135deg,#fff3df,#ffdfd0)"
    if code in (95, 96, 99):                         # thunderstorm
        return "linear-gradient(135deg,#ecedfb,#dfe3f7)"
    if code in (71, 73, 75, 77, 85, 86):             # snow
        return "#eef4fb"
    if precip >= 50 or code in (51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82):  # rain
        return "linear-gradient(135deg,#eaf4fe,#dcebfb)"
    if code in (45, 48):                             # fog
        return "#f1f3f4"
    if code == 3:                                    # overcast
        return "#f4f5f7"
    if code in (1, 2):                               # mostly/partly cloudy
        return "#fcfdf3"
    return "linear-gradient(135deg,#fffdef,#fff4cf)"  # clear / sunny


def _render_day_card(day: dict, today: bool = False) -> str:
    dw = day.get("weather") or {}
    wx = (f"&nbsp;&nbsp;<span style='color:#999;font-weight:400;font-size:12px'>"
          f"{dw.get('emoji','')} {dw.get('high_f','')}°·{dw.get('precip_pct',0)}%</span>") if dw else ""
    rows = ""
    for c in day.get("conflicts", []):
        rows += f"<div class='conflict'>⚠️ {c['a']} ({c['a_time']}) vs {c['b']} ({c['b_time']})</div>"
    if day["events"]:
        for e in day["events"]:
            loc = f"<div class='ev-loc'>📍 {e['location'][:42]}</div>" if e.get("location") else ""
            rows += (f"<div class='ev-row'><div class='ev-time'>{e['time']}</div>"
                     f"<div class='ev-body'><div class='ev-title'>{e['title']}</div>{loc}</div></div>")
    elif not day.get("conflicts"):
        rows = "<div class='empty-day'>nothing scheduled</div>"
    cls = "day-card today" if today else "day-card"
    bg = _weather_bg(dw)
    return (f"<div class='{cls}' style='background:{bg}'>"
            f"<div class='day-head'>{day['label']}{wx}</div>{rows}</div>")


# ═══════════════ Top zone: Today (left) + Needs you (right) ═══════════════

top_left, top_right = st.columns([1, 1], gap="large")

with top_left:
    st.markdown("#### 📌 Today")
    if week:
        st.markdown(_render_day_card(week[0], today=True), unsafe_allow_html=True)
    else:
        st.info("Agenda will appear after the next sync.")

with top_right:
    st.markdown("#### ✅ Needs you")
    if not pending and not all_conflicts:
        st.success("All clear — nothing needs you right now.")
    for c in all_conflicts:
        with st.container(border=True):
            st.markdown(f"⚠️ **Conflict — {c['day']}**")
            st.markdown(
                f"<div style='font-size:13px;color:#444'><b>{c['a']}</b> ({c['a_time']})"
                f" &nbsp;vs&nbsp; <b>{c['b']}</b> ({c['b_time']})</div>",
                unsafe_allow_html=True)
            st.caption("Resolve by removing one in your calendar.")
    for idx, item in enumerate(pending):
        title = item.get("title", "(untitled)")
        source = item.get("source", "")
        sender = item.get("from", "").split("<")[0].strip().strip('"') or "—"
        desc = (item.get("description") or "").replace("\r", "").strip()
        if len(desc) > 180:
            desc = desc[:180] + "…"
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(f"{source} · from {sender}")
            if desc:
                st.markdown(f"<div style='color:#555;font-size:13px;line-height:1.5'>{desc}</div>",
                            unsafe_allow_html=True)
            b1, b2, _ = st.columns([1, 1, 2])
            if b1.button("✓ Got it", key=f"y{idx}", type="primary", use_container_width=True):
                clear_item(item, pending); log_activity("acked", title, source); st.rerun()
            if b2.button("✗ Skip", key=f"n{idx}", use_container_width=True):
                clear_item(item, pending); log_activity("dismissed", title, source); st.rerun()

# ═══════════════ Rest of the week — 3-across grid ═══════════════

if len(week) > 1:
    st.markdown("#### 🗓️ Rest of the week")
    rest = week[1:]
    cols_per_row = 3
    for i in range(0, len(rest), cols_per_row):
        cols = st.columns(cols_per_row, gap="medium")
        for j, day in enumerate(rest[i:i + cols_per_row]):
            with cols[j]:
                st.markdown(_render_day_card(day), unsafe_allow_html=True)

# ─────────────────────────── coming up + activity ─────────────────────────

horizon = (dash or {}).get("horizon", {})
bdays = horizon.get("birthdays", [])
hols = horizon.get("holidays", [])
if bdays or hols:
    st.markdown("#### 🎂 Coming up")
    line = []
    for b in bdays:
        when = "today" if b["days_out"] == 0 else f"{b['days_out']}d"
        line.append(f"🎂 **{b['title']}** ({b['day']}, {when})")
    for h in hols:
        line.append(f"🎉 {h['title']} ({h['day']})")
    st.markdown(" &nbsp;·&nbsp; ".join(line))

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
    synced_txt = f"🔄 Last synced {dash['generated_label']}" if dash and dash.get("generated_label") else "🔄 Awaiting first sync"
    st.caption(f"{synced_txt} · runs 6:15 AM ET daily")
with fc2:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

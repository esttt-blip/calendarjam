"""calendarjam — command station.

A glanceable dashboard of your day and week, plus the review queue. Reads
three files from the GitHub repo via the Contents API:
  - dashboard.json      (today's plan, weather, week-ahead — written by sync)
  - pending_events.json (the review queue — interactive here)
  - activity_log.json   (recent decisions)

Tapping review buttons writes pending/synced/activity back to the repo.

Streamlit Cloud secrets needed:
  GITHUB_TOKEN  — PAT with Contents:Write on esttt-blip/calendarjam
  GITHUB_REPO   — esttt-blip/calendarjam
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
    decoded = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(decoded), data["sha"]


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
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action, "title": title[:120], "source": source,
    }
    try:
        log, sha = fetch_file("activity_log.json")
        if log is None:
            log = []
        log.append(entry)
        write_file("activity_log.json", log[-500:], sha, f"app: log {action}")
    except Exception:
        pass  # never block the user action on logging


# ─────────────────────────── page setup ───────────────────────────

st.set_page_config(page_title="calendarjam", page_icon="📅", layout="centered",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container { padding-top: 2rem; padding-bottom: 3rem; }
  div[data-testid="stMetric"] {
    background: #fff; border: 1px solid #eee; border-radius: 10px;
    padding: 10px 8px; text-align: center;
  }
  div[data-testid="stMetricLabel"] { justify-content: center; }
</style>
""", unsafe_allow_html=True)

# Load all state
dash, _ = fetch_file("dashboard.json")
pending, pending_sha = fetch_file("pending_events.json")
activity_log, _ = fetch_file("activity_log.json")
pending = pending or []
activity_log = activity_log or []

# ─────────────────────────── header ───────────────────────────

weather = (dash or {}).get("weather") or {}
hdr_left, hdr_right = st.columns([3, 2])
with hdr_left:
    st.markdown("## 📅 calendarjam")
with hdr_right:
    if weather:
        st.markdown(
            f"<div style='text-align:right;padding-top:8px'>"
            f"<span style='font-size:28px'>{weather.get('emoji','')}</span><br>"
            f"<span style='font-size:13px;color:#555'>{weather.get('high_f','–')}°/{weather.get('low_f','–')}° · "
            f"{weather.get('precip_pct',0)}% rain</span></div>",
            unsafe_allow_html=True,
        )

# ─────────────────────────── last-synced banner ───────────────────────────

now_label = datetime.now().astimezone().strftime("%-I:%M %p")
if dash and dash.get("generated_label"):
    st.markdown(
        f"<div style='background:#f0f4ff;border-radius:8px;padding:8px 12px;"
        f"font-size:12px;color:#445;margin-bottom:12px'>"
        f"🔄 <b>Last synced:</b> {dash['generated_label']} &nbsp;·&nbsp; "
        f"<span style='color:#889'>page opened {now_label}</span></div>",
        unsafe_allow_html=True,
    )
else:
    st.warning("No dashboard snapshot yet — the next 6:15 AM sync will populate this.", icon="⏳")

# ─────────────────────────── metrics row ───────────────────────────

counts = (dash or {}).get("counts", {})
m1, m2, m3 = st.columns(3)
m1.metric("🔴 To review", len(pending))
m2.metric("📅 Today", counts.get("events_today", len((dash or {}).get("today", []))))
m3.metric("⚠️ Conflicts", counts.get("conflicts_week", 0))

# ─────────────────────────── week-ahead alerts ───────────────────────────

wk = (dash or {}).get("week_ahead", {})
collisions = wk.get("collisions", [])
tight = wk.get("tight", [])
wx = wk.get("weather", [])

if collisions or tight or wx:
    for c in collisions:
        st.error(f"**{c['day']}** — {c['a']} ({c['a_time']}) overlaps {c['b']} ({c['b_time']})", icon="⚠️")
    for t in tight:
        st.warning(f"**{t['day']}** — only {t['gap_min']} min between {t['a']} and {t['b']}", icon="⏱️")
    for w in wx:
        st.info(f"**{w['day']}** — {w['title']} at {w['time']} · {w['precip_pct']}% rain, possible cancellation",
                icon=w.get("emoji", "🌧️"))

# ─────────────────────────── tabs ───────────────────────────

tab_review, tab_today, tab_week, tab_log = st.tabs(
    [f"🔴 Review ({len(pending)})", "📅 Today", "🗓️ Week", "📜 Activity"]
)

# ---- Review queue ----
with tab_review:
    if not pending:
        st.success("🎉 Queue clear — nothing to review.")
    else:
        st.caption("Tap ✓ once handled, or ✗ to dismiss.")
        for idx, item in enumerate(pending):
            title = item.get("title", "(untitled)")
            source = item.get("source", "")
            sender = item.get("from", "").split("<")[0].strip().strip('"') or "—"
            desc = (item.get("description") or "").replace("\r", "").strip()
            if len(desc) > 240:
                desc = desc[:240] + "…"
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(f"{source} · from {sender}")
                if desc:
                    st.markdown(f"<div style='color:#555;font-size:13px;line-height:1.5'>{desc}</div>",
                                unsafe_allow_html=True)
                c1, c2, _ = st.columns([1, 1, 3])
                if c1.button("✓ Got it", key=f"y{idx}", type="primary", use_container_width=True):
                    synced, ssha = fetch_file("synced_events.json")
                    synced = synced or []
                    if item["id"] not in synced:
                        synced.append(item["id"]); synced.sort()
                    write_file("synced_events.json", synced, ssha, f"app: got it — {title[:40]}")
                    _, psha = fetch_file("pending_events.json")
                    write_file("pending_events.json", [p for p in pending if p["id"] != item["id"]],
                               psha, f"app: clear — {title[:40]}")
                    log_activity("acked", title, source)
                    st.rerun()
                if c2.button("✗ Skip", key=f"n{idx}", use_container_width=True):
                    synced, ssha = fetch_file("synced_events.json")
                    synced = synced or []
                    if item["id"] not in synced:
                        synced.append(item["id"]); synced.sort()
                    write_file("synced_events.json", synced, ssha, f"app: skip — {title[:40]}")
                    _, psha = fetch_file("pending_events.json")
                    write_file("pending_events.json", [p for p in pending if p["id"] != item["id"]],
                               psha, f"app: drop — {title[:40]}")
                    log_activity("dismissed", title, source)
                    st.rerun()

# ---- Today ----
with tab_today:
    today = (dash or {}).get("today_with_drives") or (dash or {}).get("today", [])
    if not today:
        st.info("No events today.")
    else:
        for e in today:
            cols = st.columns([1, 4])
            cols[0].markdown(f"**{e['time']}**")
            line = f"**{e['title']}**"
            if e.get("outdoor") and weather and weather.get("precip_pct", 0) >= 40:
                line += f" · ☔ {weather['precip_pct']}%"
            cols[1].markdown(line)
            if e.get("location"):
                cols[1].caption(f"📍 {e['location']}")

# ---- Week ahead ----
with tab_week:
    horizon = wk.get("horizon", {})
    bdays = horizon.get("birthdays", [])
    hols = horizon.get("holidays", [])
    if bdays:
        st.markdown("**🎂 Birthdays**")
        for b in bdays:
            when = "today" if b["days_out"] == 0 else f"in {b['days_out']} days"
            st.markdown(f"- {b['title']} — {b['day']} _({when})_")
    if hols:
        st.markdown("**🎉 Holidays**")
        for h in hols:
            st.markdown(f"- {h['title']} — {h['day']}")
    if not (bdays or hols or collisions or tight or wx):
        st.info("Clear week ahead — no conflicts or notable dates.")

# ---- Activity ----
with tab_log:
    if not activity_log:
        st.info("No recent activity.")
    else:
        recent = sorted(activity_log, key=lambda e: e.get("ts", ""), reverse=True)[:25]
        icons = {"added": "✓", "acked": "✓", "dismissed": "✗", "skipped": "✗"}
        for e in recent:
            try:
                d = datetime.fromisoformat(e["ts"]).astimezone().strftime("%b %-d")
            except Exception:
                d = "—"
            icon = icons.get(e.get("action"), "·")
            st.markdown(f"<div style='font-size:13px;padding:2px 0;color:#555'>"
                        f"<span style='color:#999'>{d}</span> &nbsp; {icon} {e.get('title','')[:60]}"
                        f"</div>", unsafe_allow_html=True)

# ─────────────────────────── refresh ───────────────────────────

st.divider()
if st.button("🔄 Refresh", use_container_width=True):
    st.rerun()
st.caption("Sync runs 6:15 AM ET daily. Dashboard updates each sync.")

"""Minimal Streamlit review app for calendarjam.

Reads pending_events.json + synced_events.json from the GitHub repo via the
Contents API. When the user taps a button, updates both files and commits
back to the repo. Daily sync continues to add new pending items.

Deploy on Streamlit Cloud (private app). Secrets needed:
  - GITHUB_TOKEN: PAT with Contents:Write on esttt-blip/calendarjam
  - GITHUB_REPO:  esttt-blip/calendarjam
"""

from __future__ import annotations

import base64
import json
from datetime import datetime

import requests
import streamlit as st

# ─────────────────────────── config ───────────────────────────

REPO = st.secrets.get("GITHUB_REPO", "esttt-blip/calendarjam")
TOKEN = st.secrets["GITHUB_TOKEN"]
BRANCH = "main"

API_BASE = f"https://api.github.com/repos/{REPO}/contents"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ─────────────────────────── github helpers ───────────────────────────


def fetch_file(path: str) -> tuple[list, str]:
    """Return (parsed_json, sha) for a JSON file at path. sha needed for write."""
    r = requests.get(f"{API_BASE}/{path}", headers=HEADERS, params={"ref": BRANCH}, timeout=15)
    r.raise_for_status()
    data = r.json()
    decoded = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(decoded), data["sha"]


def write_file(path: str, content_obj, sha: str, message: str) -> None:
    """Commit content_obj as JSON to path. Must supply current sha."""
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(content_obj, indent=2).encode()).decode(),
        "sha": sha,
        "branch": BRANCH,
    }
    r = requests.put(f"{API_BASE}/{path}", headers=HEADERS, json=body, timeout=15)
    r.raise_for_status()


# ─────────────────────────── page ───────────────────────────

st.set_page_config(
    page_title="calendarjam",
    page_icon="📅",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.title("📅 calendarjam")
st.caption(f"Review queue · {datetime.now().strftime('%a %b %-d')}")

# Load state
try:
    pending, pending_sha = fetch_file("pending_events.json")
    synced, synced_sha = fetch_file("synced_events.json")
except requests.HTTPError as e:
    st.error(f"Couldn't load state from GitHub: {e}")
    st.stop()

if not pending:
    st.success("🎉 Empty queue. Nothing to review.")
    st.stop()

st.write(f"**{len(pending)} item{'s' if len(pending) != 1 else ''} pending**")

# Show each item with action buttons
for idx, item in enumerate(pending):
    title = item.get("title", "(untitled)")
    source = item.get("source", "")
    sender = item.get("from", "").split("<")[0].strip().strip('"') or "—"
    description = (item.get("description") or "").replace("\r", "").strip()
    if len(description) > 280:
        description = description[:280] + "…"

    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(f"{source} · from {sender}")
        if description:
            st.markdown(
                f"<div style='color:#555;font-size:13px;line-height:1.5'>{description}</div>",
                unsafe_allow_html=True,
            )

        col1, col2, col3 = st.columns([1, 1, 4])

        with col1:
            if st.button("✓ Got it", key=f"yes_{idx}", use_container_width=True, type="primary"):
                # Mark as synced (user confirmed they handled it)
                if item["id"] not in synced:
                    synced.append(item["id"])
                    synced.sort()
                new_pending = [p for p in pending if p["id"] != item["id"]]
                write_file("synced_events.json", synced, synced_sha, f"app: got it — {title[:50]}")
                # Re-fetch sha for pending after first write to avoid stale-sha errors
                _, pending_sha = fetch_file("pending_events.json")
                write_file("pending_events.json", new_pending, pending_sha, f"app: clear pending — {title[:50]}")
                st.success(f"✓ Marked done: {title[:60]}")
                st.rerun()

        with col2:
            if st.button("✗ Skip", key=f"no_{idx}", use_container_width=True):
                if item["id"] not in synced:
                    synced.append(item["id"])
                    synced.sort()
                new_pending = [p for p in pending if p["id"] != item["id"]]
                write_file("synced_events.json", synced, synced_sha, f"app: skip — {title[:50]}")
                _, pending_sha = fetch_file("pending_events.json")
                write_file("pending_events.json", new_pending, pending_sha, f"app: drop — {title[:50]}")
                st.info(f"Skipped: {title[:60]}")
                st.rerun()

# Footer
st.divider()
st.caption(
    "Daily sync runs at 6:15 AM ET. New items appear here automatically. "
    "Tap **✓ Got it** when you've added the event to your calendar, "
    "or **✗ Skip** to dismiss."
)

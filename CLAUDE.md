# calendarjam

Calendar sync that runs in the cloud, not on this Mac. The daily flow is:

1. **GitHub Actions** fires `sync.py` every morning at 6 AM ET
2. High-confidence events (ICS feeds, school holidays, etc.) → written **directly** to Google Calendar
3. Ambiguous Gmail items → emailed to Esther as a daily summary with numbered review items
4. **Reply handler** polls Gmail every 5 minutes for replies like `no 1` / `yes 2,3` / `1 is already on the calendar` and updates the queue

Esther does her review by replying to the summary email from her phone. She does **not** open a Claude Code session for daily review anymore.

---

## When to use this project locally (Mac)

Only for **maintenance and debugging**, not for daily review:

- **Add a new ICS feed** — edit `config.json`, commit, push. Done.
- **Tweak classifier rules** — edit `classify.py` (e.g. "always auto-add this source", "always email this source"). Push.
- **Debug a failing sync** — run `python sync.py` locally, inspect output. The local run uses `.env` for credentials and `.google_token.json` for OAuth.
- **Re-run OAuth flow** — `python auth.py` (only if Google revokes the refresh token, which is rare).
- **Bulk fix calendar state** — write a one-off script using `calendar_api.py` for unusual cleanup.

If a session opens here and the user asks about "the daily review," remind them that's now handled via email — don't try to run the old interactive walkthrough.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions (esttt-blip/calendarjam)                     │
│                                                               │
│  .github/workflows/sync.yml      → 6 AM ET daily             │
│  .github/workflows/replies.yml   → every 5 min               │
│                                                               │
│  Secrets:                                                     │
│    GMAIL_ADDRESS, GMAIL_APP_PASSWORD                          │
│    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN │
└──────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│  sync.py — daily run                                         │
│   ├─ fetch_ics_feed()       ← VIVA, Ignite, Swanson         │
│   ├─ fetch_gmail_events()   ← IMAP scan of inbox            │
│   ├─ classify.classify()    ← auto / review / skip          │
│   ├─ calendar_api.create_event()  ← writes auto items       │
│   └─ send_summary_email()   ← daily digest                  │
│                                                               │
│  reply_handler.py — every 5 min                              │
│   ├─ fetch_replies()        ← IMAP search of inbox          │
│   ├─ parse_reply()          ← strict + natural language     │
│   ├─ send_ack()             ← ✓ confirmation                │
│   └─ send_didnt_understand() ← if parse fails               │
│                                                               │
│  State files (committed back to repo by Actions):            │
│   pending_events.json       ← items awaiting reply          │
│   synced_events.json        ← processed IDs (dedup)         │
│   last_summary.json         ← message-id of last digest     │
│   processed_replies.json    ← reply message-ids already seen │
└──────────────────────────────────────────────────────────────┘
```

---

## Source-specific rules (in `classify.py`)

| Source | Behavior |
|--------|----------|
| Swanson 📚 | Auto-add. Title reformatted (`📚 School Holiday (X)` / `📚 Early Release`). 9 AM – 5 PM, free transparency, work email added as attendee. |
| VIVA ⚽ / Ignite ⚾ | Auto-add. If location missing from ICS, description gets a "⚠️ verify field" note. Weekday daytime games auto-add work attendee. |
| Gmail invite (`.ics` attachment) | Auto-add. **Skip** titles starting with "Accepted:" / "Declined:" / "Tentative:" — those are response notifications, not real invites. |
| Gmail (event-like body text) | **Review** — sent in daily summary email. User replies yes/no. |

Drive-time blocks (Spirit games, airport flights) are **not yet implemented** in the cloud version — those were interactive in the old workflow.

---

## Files

| File | Purpose |
|------|---------|
| `sync.py` | Daily fetch + classify + write loop |
| `reply_handler.py` | Polls for replies, parses commands |
| `classify.py` | Per-source rules (auto vs review vs skip) |
| `calendar_api.py` | Thin wrapper around Google Calendar API |
| `auth.py` | OAuth credentials loader (env-based for cloud, file-based locally) |
| `config.json` | ICS feed URLs and filter rules |
| `.github/workflows/*.yml` | Scheduled runners |
| `.env` (gitignored) | Local-only credentials |
| `google_credentials.json` (gitignored) | Local-only OAuth client secret |
| `.google_token.json` (gitignored) | Local-only refresh token cache |

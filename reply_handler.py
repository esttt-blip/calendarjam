#!/usr/bin/env python3
"""calendarjam reply handler.

Polls Gmail every 5 minutes for replies to the most recent summary email.
Parses 'yes 1, 3' / 'no 2' / 'yes all' commands. Updates the pending queue.

v1 scope:
- 'no N'   → dismiss item N (mark synced, drop from queue)
- 'yes N'  → acknowledge (item left in queue, user adds manually). Reply with
             a hint that smart auto-add is coming.

v2 (planned): parse date/time/location from the original email body and
auto-create the calendar event.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
PENDING_FILE = BASE_DIR / "pending_events.json"
SYNCED_FILE = BASE_DIR / "synced_events.json"
LAST_SUMMARY_FILE = BASE_DIR / "last_summary.json"
PROCESSED_REPLIES_FILE = BASE_DIR / "processed_replies.json"

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass


def decode_subject(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ─────────────────────────── reply parsing ───────────────────────────


_STRICT_CMD_RE = re.compile(
    # verb followed by numbers (with optional separators: comma, space, dash, "and", "or")
    r"\b(yes|no|dismiss|keep|add|skip|drop|ignore|remove)\b\s+"
    r"(all|(?:\d+(?:\s*(?:,|-|and|or|\s)\s*\d+)*))",
    re.IGNORECASE,
)

# Natural language sentiment keywords (whole words)
_NEG_KEYWORDS = re.compile(
    r"\b(no|not|skip|drop|ignore|dismiss|delete|remove|already|done|nope|cancel|trash|nah)\b",
    re.IGNORECASE,
)
_POS_KEYWORDS = re.compile(
    r"\b(yes|yeah|yep|yup|add|create|schedule|please|want|sure)\b",
    re.IGNORECASE,
)
_ALL_KEYWORD = re.compile(r"\ball\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\b(\d+)\b")


def _strip_quoted(body: str) -> str:
    """Cut off quoted email content (everything after the first quote marker)."""
    cutoffs = ["\n>", "\n-----Original", "\nOn ", "\nFrom: ", "\n________"]
    for c in cutoffs:
        i = body.find(c)
        if i > 0:
            return body[:i]
    return body


def _parse_strict(body: str, n_items: int) -> dict:
    """Match exact 'yes 1, 3' / 'no 2' / 'skip 1' patterns."""
    result: dict = {"yes": [], "no": []}

    yes_verbs = {"yes", "add", "keep"}

    for match in _STRICT_CMD_RE.finditer(body):
        verb = match.group(1).lower()
        nums_raw = match.group(2).strip()

        bucket = "yes" if verb in yes_verbs else "no"

        if nums_raw == "all":
            result[bucket] = list(range(1, n_items + 1))
            continue

        for chunk in re.split(r"[,\s]+|and|or", nums_raw, flags=re.IGNORECASE):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                try:
                    lo, hi = chunk.split("-")
                    result[bucket].extend(range(int(lo), int(hi) + 1))
                except ValueError:
                    pass
            else:
                try:
                    result[bucket].append(int(chunk))
                except ValueError:
                    pass

    return result


def _parse_natural(body: str, n_items: int) -> dict:
    """Best-effort fallback — scan each sentence for numbers + sentiment.

    Per sentence: if numbers mentioned AND only positive (or only negative)
    sentiment keywords present, classify the numbers accordingly. If both
    or neither, skip (ambiguous).
    """
    result: dict = {"yes": [], "no": []}

    # Split into sentences/lines. Period, !, ?, newline are sentence breaks.
    sentences = re.split(r"[.!?\n]+", body)

    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue

        nums = [int(m) for m in _NUM_RE.findall(s)]
        has_all = bool(_ALL_KEYWORD.search(s))

        is_neg = bool(_NEG_KEYWORDS.search(s))
        is_pos = bool(_POS_KEYWORDS.search(s))

        # Need clear sentiment, otherwise skip
        if is_neg == is_pos:
            continue

        bucket = "no" if is_neg else "yes"

        if has_all and not nums:
            result[bucket] = list(range(1, n_items + 1))
            continue

        result[bucket].extend(n for n in nums if 1 <= n <= n_items)

    return result


def parse_reply(body: str, n_items: int) -> dict:
    """Extract commands from reply text.

    Returns {'yes': [1,3], 'no': [2]} — 1-indexed.
    Tries strict pattern first, falls back to natural-language scan.
    """
    body = _strip_quoted(body)

    result = _parse_strict(body, n_items)

    # If strict found nothing, try natural language
    if not result["yes"] and not result["no"]:
        result = _parse_natural(body, n_items)

    # Dedup + clamp
    for bucket in result:
        result[bucket] = sorted({n for n in result[bucket] if 1 <= n <= n_items})

    return result


# ─────────────────────────── gmail i/o ───────────────────────────


def fetch_replies(target_message_id: str | None) -> list[dict]:
    """Find unread replies that thread to the most recent summary email.

    Matches via In-Reply-To header or by subject containing 'calendarjam'.
    Returns list of {uid, message_id, body} dicts.
    """
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        return []

    replies = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, app_password)
        mail.select("inbox")

        # Search recent inbox for Re: calendarjam
        _, msg_ids = mail.search(None, '(SUBJECT "calendarjam")')

        for msg_id in msg_ids[0].split():
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw = data[0][1]
            msg = email.message_from_bytes(raw)

            subject = decode_subject(str(msg.get("Subject", "")))
            in_reply_to = str(msg.get("In-Reply-To", ""))
            this_msg_id = str(msg.get("Message-ID", ""))
            sender = str(msg.get("From", ""))

            # Only consider replies (subject starts with Re:)
            if not subject.lower().startswith("re:"):
                continue
            # Only from user themselves
            if gmail_address not in sender:
                continue
            # Optional: prefer threaded replies
            if target_message_id and target_message_id not in in_reply_to:
                # Still include — Gmail clients sometimes strip In-Reply-To
                pass

            # Extract text body
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")
                    break

            replies.append({
                "uid": msg_id.decode(),
                "message_id": this_msg_id,
                "body": body,
            })

        mail.logout()
    except Exception as e:
        print(f"  [Gmail] error: {e}")

    return replies


def send_ack(dismissed_titles: list[str], acknowledged_titles: list[str]) -> None:
    """Send a confirmation that the reply was processed successfully."""
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        return

    lines = ["✓ calendarjam reply processed.\n"]
    if dismissed_titles:
        lines.append("Dismissed:")
        lines += [f"  • {t}" for t in dismissed_titles]
        lines.append("")
    if acknowledged_titles:
        lines.append("Marked yes (auto-add coming in v2 — add manually for now):")
        lines += [f"  • {t}" for t in acknowledged_titles]

    msg = MIMEText("\n".join(lines), "plain")
    msg["Subject"] = "📅 calendarjam — reply processed"
    msg["From"] = f"calendarjam <{gmail_address}>"
    msg["To"] = gmail_address
    _send(msg)


def send_didnt_understand(body_snippet: str, pending_titles: list[str]) -> None:
    """Tell the user we got their reply but couldn't parse a command."""
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_address or not app_password:
        return

    item_list = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(pending_titles))

    body = f"""Hi — I got your reply but couldn't find a yes/no command in it.

Your reply started with:
  "{body_snippet[:120]}…"

Please reply with one of:
  yes 1, 3    → add items 1 and 3 to your calendar
  no 2        → dismiss item 2
  yes all     → add everything
  no all      → dismiss everything

Pending items right now:
{item_list}

(I left everything in the queue — reply again with the format above.)
"""
    msg = MIMEText(body, "plain")
    msg["Subject"] = "📅 calendarjam — didn't understand your reply"
    msg["From"] = f"calendarjam <{gmail_address}>"
    msg["To"] = gmail_address
    _send(msg)


def _send(msg) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.send_message(msg)
        print(f"  ✓ sent: {msg['Subject']}")
    except Exception as e:
        print(f"  ✗ send failed: {e}")


# ─────────────────────────── main ───────────────────────────


def main():
    last_summary = load_json(LAST_SUMMARY_FILE, None)
    if not last_summary:
        print("No summary email on record — nothing to match against.")
        return

    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("Pending queue empty — nothing to do.")
        return

    synced_ids: set = set(load_json(SYNCED_FILE, []))
    processed_reply_ids: set = set(load_json(PROCESSED_REPLIES_FILE, []))

    replies = fetch_replies(last_summary.get("message_id"))
    new_replies = [r for r in replies if r["message_id"] not in processed_reply_ids]

    if not new_replies:
        print("No new replies.")
        return

    print(f"Processing {len(new_replies)} reply(ies)...")

    n_items = len(pending)
    all_dismissed_indices: set = set()
    all_acknowledged_indices: set = set()

    unparseable: list[dict] = []

    for reply in new_replies:
        cmds = parse_reply(reply["body"], n_items)
        print(f"  reply: {cmds}")
        if not cmds["yes"] and not cmds["no"]:
            unparseable.append(reply)
        all_dismissed_indices.update(cmds["no"])
        all_acknowledged_indices.update(cmds["yes"])
        processed_reply_ids.add(reply["message_id"])

    # Apply dismissals: remove from pending, add to synced
    dismissed_titles = []
    if all_dismissed_indices:
        new_pending = []
        for i, item in enumerate(pending, start=1):
            if i in all_dismissed_indices:
                synced_ids.add(item["id"])
                dismissed_titles.append(item.get("title", "Untitled"))
            else:
                new_pending.append(item)
        pending = new_pending

    # Acknowledgements: keep in queue (v1) but record titles for ack email
    acknowledged_titles = [
        pending[i - 1].get("title", "Untitled")
        for i in all_acknowledged_indices
        if 1 <= i <= len(pending)
    ]

    # Persist state — note we save processed_reply_ids even for unparseable replies
    # so we don't spam the user with the same "didn't understand" email.
    save_json(SYNCED_FILE, sorted(synced_ids))
    save_json(PENDING_FILE, pending)
    save_json(PROCESSED_REPLIES_FILE, sorted(processed_reply_ids))

    # Confirmation email for successful actions
    if dismissed_titles or acknowledged_titles:
        send_ack(dismissed_titles, acknowledged_titles)

    # "Didn't understand" email for failed parses (only if no successful actions)
    if unparseable and not (dismissed_titles or acknowledged_titles):
        first = unparseable[0]
        # Trim the body to just the user's actual text (before quoted email)
        body = first["body"]
        for cutoff in ["\n>", "\n-----Original", "\nOn ", "\nFrom: ", "\n________"]:
            i = body.find(cutoff)
            if i > 0:
                body = body[:i]
                break
        snippet = body.strip()
        send_didnt_understand(snippet, [p.get("title", "Untitled") for p in pending])

    print(f"\nDone. {len(dismissed_titles)} dismissed, {len(acknowledged_titles)} acknowledged, {len(unparseable)} unparseable.")


if __name__ == "__main__":
    main()

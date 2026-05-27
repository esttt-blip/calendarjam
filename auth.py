"""Google OAuth helper for calendarjam.

Two modes:
- Local one-time setup: run `python auth.py` to do the consent flow and save a refresh token
- Cloud runtime: load credentials from env vars set as GitHub secrets

The refresh token is long-lived. Once obtained locally, it's stored as a GitHub
secret and used by the Actions runs to mint short-lived access tokens.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = Path(__file__).parent / ".google_token.json"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"


def get_credentials_from_env() -> Credentials:
    """Build Credentials from GitHub secrets (the runtime path)."""
    refresh_token = os.environ["GOOGLE_REFRESH_TOKEN"]
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def get_credentials_from_file() -> Credentials:
    """Local-dev path: read cached token or run consent flow."""
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds

    if not CREDENTIALS_FILE.exists():
        sys.exit(
            f"Missing {CREDENTIALS_FILE.name}. Download it from Google Cloud Console:\n"
            "APIs & Services → Credentials → your OAuth client → Download JSON"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def get_credentials() -> Credentials:
    """Choose the right loader based on environment."""
    if "GOOGLE_REFRESH_TOKEN" in os.environ:
        return get_credentials_from_env()
    return get_credentials_from_file()


if __name__ == "__main__":
    creds = get_credentials_from_file()
    print("\n✅ OAuth complete. Tokens saved to .google_token.json")
    print("\nFor GitHub secrets, use these values:\n")

    with open(CREDENTIALS_FILE) as f:
        creds_json = json.load(f)
    installed = creds_json.get("installed") or creds_json.get("web", {})

    print(f"GOOGLE_CLIENT_ID:     {installed['client_id']}")
    print(f"GOOGLE_CLIENT_SECRET: {installed['client_secret']}")
    print(f"GOOGLE_REFRESH_TOKEN: {creds.refresh_token}")

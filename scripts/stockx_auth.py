#!/usr/bin/env python3
"""One-time StockX OAuth2 Authorization Code flow helper.

Run this ONCE to obtain your initial refresh_token. After that, the backend
auto-refreshes the access_token using the stored refresh_token.

Usage
-----
1. Set CLIENT_ID, CLIENT_SECRET, REDIRECT_URI below (or as env vars).
2. Run:  python scripts/stockx_auth.py
3. Copy the printed STOCKX_REFRESH_TOKEN value into your .env file.

You only need to re-run this if the refresh_token is ever revoked (e.g. you
delete + recreate your StockX app, or the session expires after very long
inactivity).
"""

from __future__ import annotations

import os
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

# ── Config ─────────────────────────────────────────────────────────────────
CLIENT_ID = os.environ.get("STOCKX_CLIENT_ID", "YOUR_CLIENT_ID")
CLIENT_SECRET = os.environ.get("STOCKX_CLIENT_SECRET", "YOUR_CLIENT_SECRET")

# This must match EXACTLY what you put in the StockX developer portal.
REDIRECT_URI = "http://localhost:8765/callback"

AUTH_DOMAIN = "https://accounts.stockx.com"
AUDIENCE = "gateway.stockx.com"
SCOPE = "offline_access openid"
STATE = "loupe_stockx_auth_12345"

# ── Step 1: Build authorization URL ────────────────────────────────────────

params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPE,
    "audience": AUDIENCE,
    "state": STATE,
}

auth_url = f"{AUTH_DOMAIN}/authorize?" + urllib.parse.urlencode(params)

print("\n" + "=" * 60)
print("STOCKX OAUTH2 SETUP — one-time flow")
print("=" * 60)
print(f"\nOpening browser to authorize:\n{auth_url}\n")
webbrowser.open(auth_url)

# ── Step 2: Local callback server to capture the code ──────────────────────

auth_code: str | None = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        returned_state = (qs.get("state") or [""])[0]
        if returned_state != STATE:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch - CSRF guard triggered. Try again.")
            return

        auth_code = (qs.get("code") or [""])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorization successful!</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args: object) -> None:  # silence default access logs
        pass


print("Waiting for StockX to redirect back to http://localhost:8765/callback ...")
server = HTTPServer(("localhost", 8765), CallbackHandler)
server.handle_request()  # blocks until one request arrives

if not auth_code:
    print("\n❌ No authorization code received. Did you accept the consent screen?")
    raise SystemExit(1)

print(f"✅ Authorization code received: {auth_code[:12]}...")

# ── Step 3: Exchange code for tokens ───────────────────────────────────────

print("\nExchanging authorization code for tokens...")

resp = httpx.post(
    f"{AUTH_DOMAIN}/oauth/token",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
    },
)

if resp.status_code >= 400:
    print(f"\n❌ Token exchange failed: HTTP {resp.status_code}")
    print(resp.text)
    raise SystemExit(1)

tokens = resp.json()
refresh_token = tokens.get("refresh_token", "")
access_token = tokens.get("access_token", "")

# ── Step 4: Print env vars ─────────────────────────────────────────────────

print("\n" + "=" * 60)
print("SUCCESS — add these to your .env file:")
print("=" * 60)
print(f"\nSTOCKX_CLIENT_ID={CLIENT_ID}")
print(f"STOCKX_CLIENT_SECRET={CLIENT_SECRET}")
print(f"STOCKX_REFRESH_TOKEN={refresh_token}")
print("\n# STOCKX_API_KEY — copy from https://developer.stockx.com → Keys page")
print("STOCKX_API_KEY=YOUR_API_KEY_FROM_PORTAL")
print("\nNote: Never commit these values to git.")
print("=" * 60 + "\n")

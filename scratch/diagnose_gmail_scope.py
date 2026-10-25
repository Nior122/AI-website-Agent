"""Diagnose exactly which scopes the current refresh token grants.

Reads credentials from .env.local, exchanges the refresh token for an access token,
then checks the access token at Google's tokeninfo endpoint to see what scopes it
actually carries. This tells us whether the refresh token was minted with gmail.send.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

def load_env_local():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip().strip('"').strip("'")
    return env

def main():
    env = load_env_local()

    client_id = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID", "")
    client_secret = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET", "")
    refresh_token = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", "")

    print("=" * 60)
    print("Gmail Scope Diagnostic")
    print("=" * 60)
    print(f"Client ID present:      {bool(client_id)} ({client_id[:20]}...)" if client_id else "Client ID: MISSING")
    print(f"Client Secret present:  {bool(client_secret)}")
    print(f"Refresh Token present:  {bool(refresh_token)} ({refresh_token[:15]}...)" if refresh_token else "Refresh Token: MISSING")
    print()

    if not all([client_id, client_secret, refresh_token]):
        print("ERROR: Missing credentials. Cannot proceed.")
        sys.exit(1)

    # Step 1: Exchange refresh token for access token
    print("Step 1: Exchanging refresh token for access token...")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR: Token exchange failed: {e}")
        sys.exit(1)

    access_token = token_data.get("access_token")
    scope_from_exchange = token_data.get("scope", "")
    print(f"  Access token received: {bool(access_token)}")
    print(f"  Scope from token exchange response: '{scope_from_exchange}'")
    print(f"  Expires in: {token_data.get('expires_in')} seconds")
    print(f"  Token type: {token_data.get('token_type')}")
    print()

    # Step 2: Check token at tokeninfo endpoint
    print("Step 2: Checking access token at Google's tokeninfo endpoint...")
    info_url = f"{TOKENINFO_URL}?access_token={urllib.parse.quote(access_token)}"
    try:
        with urllib.request.urlopen(info_url) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR: Tokeninfo check failed: {e}")
        sys.exit(1)

    print(f"  Scope:    {info.get('scope', 'NONE')}")
    print(f"  Email:    {info.get('email', 'NONE')}")
    print(f"  Audience: {info.get('azp', info.get('aud', 'NONE'))}")
    print(f"  Expires:  {info.get('expires_in')} seconds")
    print()

    # Step 3: Check if gmail.send is in the scopes
    scopes = info.get("scope", "").split()
    has_send = "https://www.googleapis.com/auth/gmail.send" in scopes
    print("=" * 60)
    if has_send:
        print("RESULT: gmail.send scope IS present in the access token")
    else:
        print("RESULT: gmail.send scope is NOT present in the access token")
        print(f"Actual scopes: {scopes}")
    print("=" * 60)

    # Step 4: Try a minimal Gmail API call
    if has_send:
        print("\nStep 4: Testing Gmail API access (profile only, no send)...")
        try:
            profile_url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
            req2 = urllib.request.Request(
                profile_url,
                headers={"Authorization": f"Bearer {access_token}"},
                method="GET"
            )
            with urllib.request.urlopen(req2) as resp2:
                profile = json.loads(resp2.read().decode("utf-8"))
            print(f"  Email address: {profile.get('emailAddress')}")
            print(f"  Messages total: {profile.get('messagesTotal')}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  Profile check returned {e.code}: {body[:300]}")

if __name__ == "__main__":
    main()

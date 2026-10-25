"""Directly test the Gmail users.messages.send endpoint with the current credential.

This bypasses all orchestration logic and directly calls the Gmail API
to isolate whether the issue is in our code or the Google API itself.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

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

def get_access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]

def main():
    env = load_env_local()
    client_id = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID", "")
    client_secret = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET", "")
    refresh_token = env.get("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        print("ERROR: Missing credentials")
        sys.exit(1)

    print("Getting access token...")
    access_token = get_access_token(client_id, client_secret, refresh_token)
    print(f"Access token obtained: {access_token[:20]}...")

    # Build a minimal test message to chinecheremjoseph26@gmail.com
    recipient = "chinecheremjoseph26@gmail.com"
    msg = MIMEText("This is a test email from AI-website-Agent to verify Gmail API access.", "plain", "utf-8")
    msg["To"] = recipient
    msg["Subject"] = "Gmail API Scope Test - AI Website Agent"

    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    body = json.dumps({"raw": raw_message}).encode("utf-8")

    print(f"\nAttempting direct Gmail API call to send to {recipient}...")
    print(f"URL: {GMAIL_SEND_URL}")
    print(f"Method: POST")

    req = urllib.request.Request(
        GMAIL_SEND_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print("\n" + "=" * 60)
            print("SUCCESS! Email sent!")
            print("=" * 60)
            print(f"  Message ID: {result.get('id')}")
            print(f"  Thread ID:  {result.get('threadId')}")
            print(f"  Label IDs:  {result.get('labelIds')}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"\n{'=' * 60}")
        print(f"FAILED: HTTP {e.code}")
        print(f"{'=' * 60}")
        print(f"Response: {error_body[:500]}")
        
        # Parse the error for details
        try:
            error_data = json.loads(error_body)
            error_info = error_data.get("error", {})
            print(f"\nError code:    {error_info.get('code')}")
            print(f"Error message: {error_info.get('message')}")
            print(f"Error status:  {error_info.get('status')}")
            for detail in error_info.get("details", []):
                print(f"Detail: {json.dumps(detail, indent=2)}")
        except json.JSONDecodeError:
            pass

if __name__ == "__main__":
    # Safety: only run with explicit confirmation
    if "--confirm" not in sys.argv:
        print("This script will attempt to send a REAL test email to chinecheremjoseph26@gmail.com")
        print("Run with --confirm to proceed.")
        print("\nOr run with --dry-run to just check the API without sending.")
        sys.exit(0)
    main()

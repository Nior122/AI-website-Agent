"""Local Gmail OAuth consent helper for the gmail.send scope.

Uses Python standard library only (http.server, urllib.request, webbrowser).
No secrets are logged or sent outside Google's standard OAuth token endpoint.
"""
from __future__ import annotations

import html
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPE = "https://www.googleapis.com/auth/gmail.send"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://localhost:8080"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful!</h1><p>You may close this tab and return to your terminal.</p>")
        elif "error" in params:
            OAuthCallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>Authorization failed: {html.escape(OAuthCallbackHandler.error)}</h1>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def get_credentials():
    client_id = os.environ.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID")
    client_secret = os.environ.get("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET")

    env_local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")
    if os.path.exists(env_local) and (not client_id or not client_secret):
        with open(env_local, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID=") and not client_id:
                    client_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET=") and not client_secret:
                    client_secret = line.split("=", 1)[1].strip().strip('"').strip("'")

    if not client_id:
        client_id = input("Enter AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID: ").strip()
    if not client_secret:
        import getpass
        client_secret = getpass.getpass("Enter AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET: ").strip()

    return client_id, client_secret


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 60)
    print("Gmail OAuth Authorization Flow for 'gmail.send'")
    print("=" * 60)

    redirect_uri = os.environ.get("GMAIL_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    print(f"Redirect URI expected: {redirect_uri}")
    print("Ensure this exact redirect URI is configured in Google Cloud Console.")
    print("-" * 60)

    client_id, client_secret = get_credentials()
    if not client_id or not client_secret:
        print("Error: Client ID and Client Secret are required.")
        sys.exit(1)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{OAUTH_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    port = urllib.parse.urlparse(redirect_uri).port or 8080
    server = HTTPServer(("localhost", port), OAuthCallbackHandler)

    print("\nOpening browser for Google Sign-in & Consent...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for authorization callback on port {port}...")
    while OAuthCallbackHandler.auth_code is None and OAuthCallbackHandler.error is None:
        server.handle_request()

    if OAuthCallbackHandler.error:
        print(f"\nAuthorization failed: {OAuthCallbackHandler.error}")
        sys.exit(1)

    code = OAuthCallbackHandler.auth_code
    print("\nAuthorization code received! Exchanging for refresh token...")

    try:
        token_data = exchange_code_for_tokens(client_id, client_secret, code, redirect_uri)
    except Exception as e:
        print(f"Token exchange failed: {e}")
        sys.exit(1)

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print("Warning: Google did not return a refresh token.")
        print("If you previously authorized this client, revoke access at https://myaccount.google.com/permissions and retry with prompt=consent.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS: Refresh token minted!")
    print("=" * 60)
    
    env_local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")
    if os.path.exists(env_local):
        lines = []
        replaced = False
        with open(env_local, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN="):
                    lines.append(f"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN={refresh_token}\n")
                    replaced = True
                else:
                    lines.append(line)
        if not replaced:
            lines.append(f"\nAI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN={refresh_token}\n")
        with open(env_local, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print("Successfully updated AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN in .env.local.")
    else:
        print("\nSet this variable in your environment or .env.local:")
        print(f"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN={refresh_token}")
        print("\n(Do NOT paste this token into chat or commit it to Git.)\n")


if __name__ == "__main__":
    main()

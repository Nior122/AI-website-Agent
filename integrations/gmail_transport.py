"""The OAuth and HTTP mechanics every Gmail client in this package shares.

Why this module exists separately: Phase 48 shipped a read-only Gmail client, and Phase 49 adds a *sending* one.
Both need the same three risky things - a refresh-token exchange, a token cache, and a bounded HTTPS call that
never puts a credential in an error message. Duplicating that would mean two places to get wrong. Putting it in
one class that the read client *and* the send client inherit means the difference between them is exactly the
methods each one exposes, which is the property that matters: the read-only client has no way to send, and the
send client has no way to read a mailbox it was not authorized to read.

Two rules this module enforces rather than documents:

* **A scope is a decision, not a default.** Each subclass names the OAuth scope its credentials were minted for
  and its own environment variables. A refresh token issued for ``gmail.readonly`` cannot be silently reused for
  sending - Google would refuse it, and so does this code, because a client that quietly fell back to another
  credential would make "which mailbox may this process write to?" unanswerable.
* **Nothing secret leaves here.** Bearer tokens live in an instance attribute only; error text passes through
  :func:`orchestration.observability.redact` and is bounded. No method returns a token, and none of this is
  written to a record, a log line, or an exported workflow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from orchestration.observability import redact

from .http import DEFAULT_TIMEOUT_SECONDS, ProviderError, provider_request

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users"
#: A refresh token is bound for the life of the process unless Google says otherwise; re-minting per request is
#: both slower and another chance to leak the secret through a log.
TOKEN_SKEW_SECONDS = 60


class GmailError(ProviderError):
    """A Gmail fault with a code an operator can act on.

    One class for both directions (read and send), because callers - the poller, the send executor, the CLI -
    already catch it, and a fault is a fault. The ``code`` says which side it came from.
    """

    def __init__(self, message: str, *, code: str, retryable: bool = False, status: int | None = None,
                 next_action: str | None = None) -> None:
        super().__init__(message, code=code, retryable=retryable, status=status)
        self.next_action = next_action


class GmailTransport:
    """Refresh-token OAuth plus one typed, bounded JSON call. Subclasses add the verbs they are allowed to use.

    ``transport`` may be injected for tests or an operator's proxy: it receives ``(method, url, params, headers,
    body)`` and returns the same ``{"status", "json", "text"}`` shape as
    :func:`integrations.http.provider_request`. With none, calls go to Google over HTTPS.
    """

    #: Subclasses name their own credentials so a read-only token can never be borrowed for a send, or the reverse.
    REFRESH_TOKEN_ENV = "AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN"
    CLIENT_ID_ENV = "AI_WEBSITE_AGENT_GMAIL_CLIENT_ID"
    CLIENT_SECRET_ENV = "AI_WEBSITE_AGENT_GMAIL_CLIENT_SECRET"
    ACCOUNT_ENV = "AI_WEBSITE_AGENT_GMAIL_ACCOUNT"
    SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
    #: Wording used in the operator's next action, so a refusal names *this* credential's scope.
    SCOPE_LABEL = "read-only"
    NOT_CONFIGURED_CODE = "GMAIL_NOT_CONFIGURED"

    def __init__(self, *, refresh_token: str, client_id: str, client_secret: str, account: str = "me",
                 api_root: str = GMAIL_API_ROOT, token_url: str = OAUTH_TOKEN_URL,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, transport: Callable[..., dict[str, Any]] | None = None,
                 request_opener: Callable[..., dict[str, Any]] | None = None) -> None:
        self.refresh_token = str(refresh_token or "").strip()
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.account = str(account or "me").strip() or "me"
        self.api_root = str(api_root or GMAIL_API_ROOT).rstrip("/")
        self.token_url = str(token_url or OAUTH_TOKEN_URL)
        self.timeout = float(timeout)
        self._transport = transport
        self._request = request_opener or provider_request
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self.calls = 0
        self.errors = 0
        #: Timestamp of the last token mint, for the config report. The token itself is never recorded.
        self.token_minted_at: str | None = None

    # ------------------------------------------------------------------ construction

    @classmethod
    def credentials_from_env(cls, env: dict[str, str] | None = None) -> dict[str, str]:
        """The three credential values for *this* client's own variables, or a coded refusal.

        Missing values are named but never guessed at: no fallback to another scope's token, no partial read
        from a differently-authorized client.
        """
        import os

        source = os.environ if env is None else env
        values = {
            "refresh_token": (source.get(cls.REFRESH_TOKEN_ENV) or "").strip(),
            "client_id": (source.get(cls.CLIENT_ID_ENV) or "").strip(),
            "client_secret": (source.get(cls.CLIENT_SECRET_ENV) or "").strip(),
            "account": (source.get(cls.ACCOUNT_ENV) or "me").strip() or "me",
        }
        missing = [name for name, key in ((cls.REFRESH_TOKEN_ENV, "refresh_token"), (cls.CLIENT_ID_ENV, "client_id"),
                                          (cls.CLIENT_SECRET_ENV, "client_secret")) if not values[key]]
        if missing:
            raise GmailError(
                f"Gmail {cls.SCOPE_LABEL} access is not configured on this machine: missing {', '.join(missing)}.",
                code=cls.NOT_CONFIGURED_CODE,
                next_action=(f"Authorize this machine with only {cls.SCOPE}, then set {', '.join(missing)}. "
                             "Nothing here can mint that consent for you: it is an account decision."),
            )
        return values

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None, **overrides: Any) -> "GmailTransport":
        arguments = cls.credentials_from_env(env)
        arguments.update(overrides)
        return cls(**arguments)

    # ------------------------------------------------------------------ the one authenticated call
    #
    # `body` is present because Google's send endpoint takes a JSON body; it is otherwise unused, and no
    # subclass other than the send adapter passes one.

    def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
                     body: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_root}/{self.account}{path}"
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        if query:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(query)}"
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        self.calls += 1
        try:
            if self._transport is not None:
                response = self._transport(method, url, query, headers, body)
            else:
                response = self._request(url, method=method, headers=headers, json_body=body,
                                          timeout=self.timeout)
        except ProviderError as error:
            self.errors += 1
            # The status is trusted alongside the code: a transport that reports 403 without naming the reason
            # still has to come back as "re-authorize", because that is the only action the operator can take.
            code = ("GMAIL_RATE_LIMITED" if error.code == "PROVIDER_RATE_LIMITED" or error.status == 429 else
                    "GMAIL_AUTHORIZATION_REFUSED" if error.code == "PROVIDER_AUTHORIZATION_REFUSED"
                    or error.status in (401, 403) else
                    "GMAIL_UNRESPONSIVE" if error.code in {"PROVIDER_UNRESPONSIVE", "PROVIDER_CALL_FAILED"} else
                    error.code)
            raise GmailError(str(error), code=code, retryable=error.retryable, status=error.status,
                             next_action=("Back off and retry: Gmail rate limits recover on their own." if code ==
                                          "GMAIL_RATE_LIMITED" else
                                          f"Re-authorize this machine with the {self.SCOPE_LABEL} scope; a refusal "
                                          "is not a problem the mailbox can fix." if code ==
                                          "GMAIL_AUTHORIZATION_REFUSED" else None)) from None
        except Exception as error:  # noqa: BLE001 - always typed, never raw.
            self.errors += 1
            raise GmailError(f"Gmail call failed: {redact(str(error))[:200]}", code="GMAIL_CALL_FAILED",
                             retryable=True) from None
        payload = response.get("json") if isinstance(response, dict) else None
        status = int((response or {}).get("status") or 0) if isinstance(response, dict) else 0
        if status and status >= 400:
            self.errors += 1
            raise GmailError(f"Gmail refused the request ({status}).", code="GMAIL_REQUEST_REFUSED", status=status,
                             retryable=status >= 500)
        if not isinstance(payload, dict):
            self.errors += 1
            raise GmailError("Gmail answered with something that is not a JSON object.",
                             code="GMAIL_RESPONSE_MALFORMED")
        return payload

    # Kept as an alias because the read-only client's callers were written against it.
    def _request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
                      body: dict[str, Any] | None = None) -> Any:
        return self.request_json(path, params=params, method=method, body=body)

    # ------------------------------------------------------------------ token handling

    def access_token(self) -> str:
        """A live bearer token, exchanged from the refresh token at most once per expiry window."""
        import time

        if self._access_token and time.time() < self._token_expires_at - TOKEN_SKEW_SECONDS:
            return self._access_token
        return self._mint_token()

    def _mint_token(self) -> str:
        import time

        if not (self.refresh_token and self.client_id and self.client_secret):
            raise GmailError("The OAuth credentials are incomplete.", code=self.NOT_CONFIGURED_CODE)
        form = {"client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"}
        try:
            response = (self._transport("POST", self.token_url, None, None, form) if self._transport is not None
                        else self._request(self.token_url, method="POST", form_body=form, timeout=self.timeout))
        except ProviderError as error:
            self.errors += 1
            raise GmailError(f"The token exchange failed: {redact(str(error))[:200]}",
                             code="GMAIL_AUTH_FAILED", retryable=bool(error.retryable),
                             status=error.status,
                             next_action="The refresh token, client id, or client secret is wrong or revoked; "
                                         f"re-authorize with the {self.SCOPE_LABEL} scope.") from None
        except Exception as error:  # noqa: BLE001
            self.errors += 1
            raise GmailError(f"The token exchange failed: {redact(str(error))[:200]}", code="GMAIL_AUTH_FAILED",
                             retryable=True) from None
        payload = response.get("json") if isinstance(response, dict) else None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            self.errors += 1
            raise GmailError("The token exchange returned no access token.", code="GMAIL_AUTH_FAILED",
                             next_action="Check that the OAuth client still exists and the refresh token was "
                                         f"issued for the {self.SCOPE_LABEL} scope.")
        self._access_token = str(payload["access_token"])
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self._token_expires_at = time.time() + max(60.0, float(expires_in))
        self.token_minted_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return self._access_token

    def forget_token(self) -> None:
        """Drop the cached bearer token. Used when a call suggests it went stale."""
        self._access_token, self._token_expires_at = None, 0.0

    def transport_status(self) -> dict[str, Any]:
        """The safe shape of this client for a report: which scope, which counters, never a secret."""
        return {"account": self.account, "scope": self.SCOPE, "token_present": bool(self._access_token),
                "token_minted_at": self.token_minted_at, "calls": self.calls, "errors": self.errors}

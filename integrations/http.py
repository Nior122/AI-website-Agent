"""One small HTTP client for provider APIs, shared by every adapter in this package.

Both Telegram and Google are HTTPS JSON APIs called with a long-lived secret in a header. Rather than let each
adapter re-invent the risky parts, this module owns them once:

* **Timeouts always.** A provider that stops answering must not hang an operator's bot or a scheduler's poll.
* **Bounded reads.** A response is read up to a byte limit and refused past it, so a broken or hostile endpoint
  cannot exhaust memory.
* **No redirects.** Each request carries a credential; following a redirect would hand it to a host the operator
  never chose. A 3xx is therefore an error, not something to obey.
* **Loopback-only plaintext.** ``http://`` is accepted only for ``127.0.0.1``/``localhost`` so tests and local
  fakes work; anything else must be ``https://``.
* **Secret-free errors.** Provider messages pass through :func:`orchestration.observability.redact`, and this
  module never puts a header value - or the bot token embedded in a URL path - into an exception.

Nothing here knows about leads, jobs, or the ledger: it is transport, and adapters compose it.
"""
from __future__ import annotations

import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, urlopen

from orchestration.observability import redact, sanitize_diagnostic

#: Seconds to wait for a provider to answer. Overridable per call; never absent.
DEFAULT_TIMEOUT_SECONDS = 15.0
#: Bytes read from a provider response before it is refused. JSON APIs answer in kilobytes.
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "[::1]", "0.0.0.0"}


class ProviderError(RuntimeError):
    """A provider call that could not be completed. Carries a stable code and whether retrying can help."""

    def __init__(self, message: str, *, code: str, retryable: bool = False,
                 status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


def check_endpoint(url: str) -> str:
    """Validate a provider URL's scheme/host and return it, refusing anything unencrypted but loopback."""
    cleaned = str(url or "").strip()
    parts = urlsplit(cleaned)
    if parts.scheme == "http":
        if (parts.hostname or "") not in LOOPBACK_HOSTS:
            raise ProviderError(f"Plaintext http is refused for '{parts.hostname}'; provider calls must use https.",
                                code="PROVIDER_URL_INSECURE")
    elif parts.scheme != "https":
        raise ProviderError(f"Only https provider endpoints are supported (got '{parts.scheme or 'no scheme'}').",
                            code="PROVIDER_URL_SCHEME_UNSUPPORTED")
    if not parts.hostname:
        raise ProviderError("The provider URL has no host.", code="PROVIDER_URL_INVALID")
    if parts.username or parts.password:
        raise ProviderError("Credentials must not be embedded in a provider URL.", code="PROVIDER_URL_INVALID")
    return cleaned


class _NoRedirects(HTTPRedirectHandler):
    """Turn any redirect into an error so an Authorization header is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib protocol
        return None

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001 - urllib protocol
        raise HTTPError(req.full_url, code, "redirect refused", headers, None)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def provider_request(url: str, *, method: str = "GET", json_body: dict[str, Any] | None = None,
                     form_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                     timeout: float = DEFAULT_TIMEOUT_SECONDS,
                     max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> dict[str, Any]:
    """Call a provider endpoint and return ``{"status", "json", "text"}``.

    Every failure leaves this function as a :class:`ProviderError` with a code, so an adapter can tell an operator
    *what kind* of trouble it hit ("timed out" vs "429, back off 30s") instead of pasting a stack trace.
    """
    target = check_endpoint(url)
    data: bytes | None = None
    request_headers = {"Accept": "application/json", "User-Agent": "ai-website-agent/1.0"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urlencode(form_body).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request_headers.update({str(key): str(value) for key, value in (headers or {}).items()})

    opener = _opener()
    request = Request(target, data=data, headers=request_headers, method=method.upper())  # noqa: S310 - https-checked above
    try:
        with opener.open(request, timeout=float(timeout)) as response:
            raw = response.read(int(max_bytes) + 1)
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as error:
        raise _map_http_error(error) from None
    except (URLError, socket.timeout, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise ProviderError(f"The provider did not answer: {redact(str(reason))[:300]}",
                            code="PROVIDER_UNRESPONSIVE", retryable=True) from None
    except (ValueError, OSError) as error:
        raise ProviderError(f"Provider call failed: {sanitize_diagnostic(error) or 'unknown fault'}",
                            code="PROVIDER_CALL_FAILED", retryable=True) from None
    if len(raw) > int(max_bytes):
        raise ProviderError(f"Provider response exceeded the {max_bytes}-byte limit.",
                            code="PROVIDER_RESPONSE_TOO_LARGE", status=status)
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "json": parsed, "text": text[:4000]}


def _opener():  # noqa: ANN202 - urllib.request.build_opener returns a private type.
    import urllib.request

    return urllib.request.build_opener(_NoRedirects())


def _map_http_error(error: HTTPError) -> ProviderError:
    """Map an HTTP failure onto a stable code, with the provider's own explanation, redacted and bounded."""
    status = int(getattr(error, "code", 0) or 0)
    body: Any = None
    try:
        raw = error.read(65_536)
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - an error body is a courtesy, never a requirement.
        body = None
    description = None
    retry_after = None
    if isinstance(body, dict):
        description = body.get("description") or body.get("error_description")
        if isinstance(body.get("parameters"), dict):
            description = description or body["parameters"].get("retry_after")
        retry_after = body.get("retry_after")
        error_field = body.get("error")
        if description is None and isinstance(error_field, dict):
            description = error_field.get("message")
        elif description is None and isinstance(error_field, str):
            description = error_field
    detail = redact(str(description or error.reason or "provider error"))[:400]
    if status == 429:
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        return ProviderError(f"Rate limited by the provider: {detail}", code="PROVIDER_RATE_LIMITED",
                             retryable=True, status=429, retry_after=retry_after)
    if status in {401, 403}:
        return ProviderError(f"The provider refused this credential: {detail}", code="PROVIDER_AUTHORIZATION_REFUSED",
                             status=status)
    if status == 404:
        return ProviderError(f"The provider has no such resource: {detail}", code="PROVIDER_NOT_FOUND", status=404)
    if 500 <= status < 600:
        return ProviderError(f"The provider reported a fault on its side: {detail}", code="PROVIDER_SERVER_ERROR",
                             retryable=True, status=status)
    return ProviderError(f"The provider rejected the request ({status}): {detail}", code="PROVIDER_REQUEST_REFUSED",
                         status=status)

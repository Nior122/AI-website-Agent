"""Telegram Bot API transport, provider details only - no leads, no jobs, no ledger.

The bot token authorizes this machine to act as one bot, so it is treated as the secret it is: read from the
environment, never stored on a record, never written to a log, and never included in an error message (the token
lives in the URL *path*, which is why :func:`integrations.http.provider_request` redacts before anything raises
and this module wraps the URL once, in a private attribute).

Endpoints used, and why each is here:

``getMe``       config check at startup: proves a token works before an operator relies on the bot.
``getUpdates``  long polling. It is the only way to receive commands without exposing a public webhook URL, and
                a webhook would need TLS plus a reachable host; a local operator bot should not require either.
``sendMessage`` replies and notifications, with an optional inline keyboard.
``editMessageText`` replaces a review prompt once its decision is recorded, so an old button cannot be pressed
                twice and expect the screen to still be honest about what it meant.
``answerCallbackQuery`` acknowledges a button press; it is *required* by the API to stop the client's spinner.

The numeric limits below are Telegram's own, and are enforced here rather than discovered in production: 4096
characters per message, 64 bytes per ``callback_data`` value, and long-poll ``timeout`` clamped to 50 seconds.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Protocol
from urllib.parse import quote

from orchestration.observability import redact

from .http import (DEFAULT_TIMEOUT_SECONDS, ProviderError, provider_request)

TELEGRAM_API_BASE = "https://api.telegram.org"
#: Environment names, chosen to match the existing ``AI_WEBSITE_AGENT_*`` convention.
TELEGRAM_TOKEN_ENV = "AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN"
TELEGRAM_USERS_ENV = "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS"
TELEGRAM_CHATS_ENV = "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS"
TELEGRAM_API_ROOT_ENV = "AI_WEBSITE_AGENT_TELEGRAM_API_ROOT"

MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_DATA_BYTES = 64
MAX_POLL_TIMEOUT_SECONDS = 50
TRUNCATION_MARK = "\n…[truncated by Telegram's message limit]"

BOT_TOKEN_PLACEHOLDER = "[REDACTED_BOT_TOKEN]"


class TelegramTransport(Protocol):
    """Anything that can POST a Bot API method and return the decoded envelope (the real one, or a fake)."""

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - interface
        ...


def telegram_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """What this machine knows about its Telegram setup, with the token never included.

    Allow-lists are parsed as comma/space separated digits. An empty list is reported as such and means *nobody
    is authorized*: the bot refuses every update, because a bot with no allow-list would be a public read of the
    operator's pipeline to whoever finds the chat.
    """
    import os

    source = os.environ if env is None else env
    token = (source.get(TELEGRAM_TOKEN_ENV) or "").strip()
    users = _id_set(source.get(TELEGRAM_USERS_ENV))
    chats = _id_set(source.get(TELEGRAM_CHATS_ENV))
    return {
        "token_present": bool(token),
        "token_length": len(token) if token else 0,
        "allowed_user_ids": sorted(users),
        "allowed_chat_ids": sorted(chats),
        "api_root": (source.get(TELEGRAM_API_ROOT_ENV) or TELEGRAM_API_BASE).strip().rstrip("/"),
        "configured": bool(token and (users or chats)),
        "code": None if (token and (users or chats)) else "TELEGRAM_NOT_CONFIGURED",
        "next_action": None if (token and (users or chats)) else (
            f"Set {TELEGRAM_TOKEN_ENV} to the token @BotFather gave you, and {TELEGRAM_USERS_ENV} (preferred) or "
            f"{TELEGRAM_CHATS_ENV} to the numeric ids allowed to operate this agent. An empty allow-list "
            "authorizes nobody."
        ),
    }


def _id_set(value: str | None) -> set[int]:
    ids: set[int] = set()
    for chunk in str(value or "").replace(",", " ").split():
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


def clip_message(text: str) -> tuple[str, bool]:
    """Fit text into one Telegram message without cutting a multi-byte character in half."""
    body = str(text or "")
    if len(body) <= MAX_MESSAGE_CHARS:
        return body, False
    room = MAX_MESSAGE_CHARS - len(TRUNCATION_MARK)
    return body[:room].rstrip() + TRUNCATION_MARK, True


def encode_callback_data(data: str) -> str:
    """Telegram bounds callback_data to 64 *bytes*; a button whose payload does not fit is a bug, not a warning."""
    encoded = str(data).encode("utf-8")
    if len(encoded) > MAX_CALLBACK_DATA_BYTES:
        raise ProviderError(
            f"Inline button payload is {len(encoded)} bytes; Telegram allows {MAX_CALLBACK_DATA_BYTES}. "
            "Reference the record by id and keep the state in the application.",
            code="TELEGRAM_CALLBACK_TOO_LARGE",
        )
    return str(data)


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """``[[("label", "callback_data"), ...], ...]`` -> Telegram's InlineKeyboardMarkup, size-checked."""
    return {"inline_keyboard": [[{"text": label[:64],
                                 "callback_data": encode_callback_data(data)} for label, data in row]
                               for row in rows]}


class TelegramBotApi:
    """Thin, safe client for the handful of Bot API methods this application uses.

    ``transport`` may be injected (a recorder in tests, or an operator's own proxy); with none, calls go to
    ``api_root`` over HTTPS with the token in the path. Nothing else about the transport is configurable, because
    each extra knob is another way to send a token somewhere unintended.
    """

    def __init__(self, token: str, *, api_root: str = TELEGRAM_API_BASE,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._token = str(token or "").strip()
        if not self._token and transport is None:
            raise ProviderError("A Telegram bot token is required before the API can be called.",
                                code="TELEGRAM_TOKEN_MISSING")
        self.api_root = str(api_root or TELEGRAM_API_BASE).rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport
        #: Counters for the operator-facing report: how many calls, how many failed, never what they said.
        self.calls = 0
        self.errors = 0

    def __repr__(self) -> str:  # never let a token reach a traceback, a log line, or a repr in a test report
        return f"<TelegramBotApi root={self.api_root!r} token={BOT_TOKEN_PLACEHOLDER} calls={self.calls}>"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        body = {key: value for key, value in (payload or {}).items() if value is not None}
        if self._transport is not None:
            try:
                response = self._transport(method, body)
            except ProviderError:
                self.errors += 1
                raise
            except Exception as error:  # noqa: BLE001 - a fake or proxy fault must still be typed.
                self.errors += 1
                raise ProviderError(f"Telegram transport failed: {redact(str(error))[:200]}",
                                    code="TELEGRAM_TRANSPORT_FAILED", retryable=True) from None
            return self._envelope(response, method)
        url = f"{self.api_root}/bot{quote(self._token, safe='')}/{method}"
        response = provider_request(url, method="POST", json_body=body, timeout=self.timeout)
        if response["status"] >= 400:
            self.errors += 1
            raise ProviderError(f"Telegram {method} was refused ({response['status']}).",
                                code="TELEGRAM_METHOD_REFUSED", status=response["status"],
                                retryable=response["status"] == 429 or response["status"] >= 500)
        return self._envelope(response["json"] if response["json"] is not None else response["text"], method)

    def _envelope(self, response: Any, method: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            self.errors += 1
            raise ProviderError(f"Telegram {method} returned something that is not a JSON object.",
                                code="TELEGRAM_RESPONSE_MALFORMED")
        if response.get("ok") is not True:
            self.errors += 1
            description = redact(str(response.get("description") or "no description"))[:300]
            retry_after = None
            if isinstance(response.get("parameters"), dict):
                retry_after = response["parameters"].get("retry_after")
            code = "TELEGRAM_RATE_LIMITED" if response.get("error_code") == 429 else "TELEGRAM_METHOD_FAILED"
            raise ProviderError(f"Telegram {method} failed: {description}", code=code,
                                retryable=code == "TELEGRAM_RATE_LIMITED",
                                retry_after=float(retry_after) if retry_after else None)
        return {"result": response.get("result"), "method": method}

    # -- methods this application uses, and nothing else ----------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return dict(self.call("getMe").get("result") or {})

    def get_updates(self, *, offset: int | None = None, limit: int = 25,
                    timeout: float | None = None) -> list[dict[str, Any]]:
        """Long-poll for updates. ``timeout`` is clamped to Telegram's 50-second ceiling."""
        payload: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if offset is not None:
            payload["offset"] = int(offset)
        if timeout is not None:
            payload["timeout"] = max(0.0, min(float(timeout), MAX_POLL_TIMEOUT_SECONDS))
        result = self.call("getUpdates", payload).get("result") or []
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None,
                     parse_mode: str | None = None, disable_notification: bool = False) -> dict[str, Any]:
        body, clipped = clip_message(text)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": body,
            # Link previews would fetch the URL from Telegram's servers; a review package path is a local
            # reference and the operator's phone does not need a preview of it.
            "link_preview_options": {"is_disabled": True},
            "disable_notification": bool(disable_notification),
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload).get("result") or {}
        return {"message_id": result.get("message_id"), "clipped": clipped, "chars": len(body)}

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str, *,
                          reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        body, clipped = clip_message(text)
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": int(message_id), "text": body,
                                  "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self.call("editMessageText", payload).get("result")
        return {"edited": bool(result), "clipped": clipped}

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None,
                              show_alert: bool = False) -> bool:
        """Acknowledge a button press. An unacknowledged press spins in the client until it gives up."""
        payload = {"callback_query_id": str(callback_query_id), "show_alert": bool(show_alert)}
        if text:
            payload["text"] = str(text)[:200]
        return bool(self.call("answerCallbackQuery", payload).get("result"))


def describe_error(error: BaseException) -> dict[str, Any]:
    """The safe shape of a provider fault for an operator answer or a durable record."""
    if isinstance(error, ProviderError):
        return {"error_code": error.code, "error": redact(str(error))[:400], "retryable": bool(error.retryable),
                **({"provider_status": error.status} if error.status else {}),
                **({"retry_after_seconds": error.retry_after} if error.retry_after else {})}
    return {"error_code": "TELEGRAM_FAULT", "error": redact(f"{type(error).__name__}: {error}")[:400],
            "retryable": True}


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

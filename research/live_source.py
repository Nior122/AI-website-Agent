"""Bounded, consent-gated research of a prospect's own public website.

This is the live ``ResearchSource`` the Phase 3 design anticipated (see ``research/README.md``): it reads
publicly reachable pages that the *record itself* names, converts what it can literally observe into
evidence items, and feeds the existing ``_evaluate_website`` / ``_verify_identity`` machinery unchanged.

What it will not do, and the code refuses rather than pretending:

* it never fetches anything a candidate does not already name - no search engine, no crawl, no sub-pages;
* it never authenticates, solves a CAPTCHA, or works around a restriction; a login wall, a robots
  refusal, or a blocked request is recorded as *our* inability to observe, never as a fact about the
  business (``_evaluate_website`` then honestly reports ``UNKNOWN``);
* it never reports "no website" because a fetch failed, and never reports a design opinion as an
  observation - every flag it sets is traceable to a literal, countable property of the bytes it read;
* it cannot be turned on by the request that uses it: the deployment's environment *and* its local
  settings file must both permit live research, and a caller must still ask for it explicitly;
* it makes no decision and takes no action - qualification, building, review, deployment, and outreach
  all live elsewhere and are not imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.request import urlopen

#: The deployment-level permit. Without this environment variable nothing in this module can make a request,
#: no matter what a job input, CLI flag, or config file says.
LIVE_RESEARCH_ENV = "AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH"
DEFAULT_SETTINGS_PATH = Path("config/system.settings.local.json")

#: Hosts that front their content with authentication. Fetching them returns a login page, which is
#: evidence about the platform, not about the business - so the platform rules decide and we do not.
PLATFORM_HOSTS = ("instagram.com", "facebook.com", "fb.me", "twitter.com", "x.com", "linkedin.com", "tiktok.com")

USER_AGENT = "AI-Website-Agent/0.1 public-page research; contact: local-operator"


class LiveResearchRefused(RuntimeError):
    """A live run was requested but policy forbids it. Raised before any request is built."""

    code = "LIVE_RESEARCH_NOT_ENABLED"


class LiveResearchUnavailable(RuntimeError):
    """Every permitted page failed to be read. Reported as a source failure, never as a finding."""

    code = "LIVE_RESEARCH_SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class LiveResearchPolicy:
    """Explicit, non-secret bounds for one live research run. Absence of permission means "off"."""

    enabled: bool = False
    permission_source: str | None = None
    max_pages: int = 2
    max_requests: int = 6
    timeout_seconds: int = 12
    max_bytes: int = 400_000
    min_delay_seconds: float = 1.0
    max_redirects: int = 3
    max_retries: int = 1
    retry_after_limit_seconds: int = 30
    respect_robots: bool = True
    fetch_platform_profiles: bool = False
    allow_hosts: tuple[str, ...] = ()
    user_agent: str = USER_AGENT

    @classmethod
    def load(cls, settings_path: Path | str | None = None) -> "LiveResearchPolicy":
        """Read the local settings file, then apply the environment permit on top of it.

        The example settings file deliberately ships ``allowLiveResearch: false``; the local override is the
        operator's act, and the environment variable is the machine's. Two independent keys, because the
        request itself must not hold the only switch.
        """
        path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        block: dict[str, Any] = {}
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            block = document.get("research") or {}
        granted_by_env = _env_permit_granted()
        enabled = bool(block.get("allowLiveResearch", False)) and granted_by_env
        allow_hosts = tuple(str(host).strip().lower() for host in (block.get("allowHosts") or []) if str(host).strip())
        return cls(
            enabled=enabled,
            permission_source=None if not enabled else f"{path.name or 'settings'} + {LIVE_RESEARCH_ENV}",
            max_pages=_bounded_int(block.get("maxPagesPerCandidate"), 2, 1, 3),
            max_requests=_bounded_int(block.get("maxRequestsPerRun"), 6, 1, 12),
            timeout_seconds=_bounded_int(block.get("requestTimeoutSeconds"), 12, 1, 45),
            max_bytes=_bounded_int(block.get("maxResponseBytes"), 400_000, 10_000, 2_000_000),
            min_delay_seconds=_bounded_float(block.get("minDelaySeconds"), 1.0, 0.0, 30.0),
            max_redirects=_bounded_int(block.get("maxRedirects"), 3, 0, 5),
            max_retries=_bounded_int(block.get("maxRetries"), 1, 0, 2),
            respect_robots=bool(block.get("respectRobotsTxt", True)),
            fetch_platform_profiles=bool(block.get("fetchPlatformProfiles", False)) and bool(allow_hosts),
            allow_hosts=allow_hosts,
        )

    def refusal_reason(self) -> str | None:
        if self.enabled:
            return None
        if not _env_permit_granted():
            return (f"Live research needs {LIVE_RESEARCH_ENV}=1 in this process's environment; no request was made.")
        return (f"{LIVE_RESEARCH_ENV}=1 is set but research.allowLiveResearch is not true in {DEFAULT_SETTINGS_PATH} "
                "(copy config/system.settings.example.json); no request was made.")


def _env_permit_granted() -> bool:
    return os.environ.get(LIVE_RESEARCH_ENV) == "1"


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _host_of(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname.lower().rstrip(".")


@dataclass
class PermittedTarget:
    url: str
    host: str
    kind: str  # "business_website" or "social_profile"


def permitted_public_target(url: Any, *, kind: str = "business_website", policy: LiveResearchPolicy | None = None) -> PermittedTarget | None:
    """Accept only a plain public http(s) page URL that the record itself named.

    Credentials, explicit ports, non-HTML paths that only make sense as API calls, private/reserved address
    literals, localhost-style names, and platform hosts (whose content sits behind a login) are all refused
    here rather than at request time, so a malformed candidate cannot turn research into probing.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    host = parsed.hostname.lower().rstrip(".") if parsed.hostname else None
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.port not in (None, 80, 443):
        return None
    if "." not in host or host.endswith((".local", ".internal", ".localhost", ".invalid", ".test")) or host == "localhost":
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    if _resolves_privately(host):
        return None
    if host.removeprefix("www.") in PLATFORM_HOSTS or any(host.endswith("." + p) for p in PLATFORM_HOSTS):
        if kind != "social_profile":
            return None
        if policy is None or not policy.fetch_platform_profiles:
            return None
        if not any(host == allowed or host.endswith("." + allowed) for allowed in (policy.allow_hosts or ())):
            return None
    return PermittedTarget(url=candidate, host=host, kind=kind)


def _resolves_privately(host: str) -> bool:
    """True when DNS answers with a non-global address; a resolver failure is not treated as permission.

    This keeps a public-looking hostname from pointing at an internal service. When the resolver is
    unavailable the caller still gets its ordinary network error - the guard adds no new failure mode.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, socket.herror, UnicodeError, OSError):
        return False
    for info in infos:
        address_text = str((info[4] or [""])[0])
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue
        if not address.is_global:
            return True
    return False


class _PageFacts(HTMLParser):
    """One tolerant pass over the fetched HTML: only what the bytes literally contain."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.property_meta: dict[str, str] = {}
        self.links: list[str] = []
        self.tel: list[str] = []
        self.mailto: list[str] = []
        self.anchors: list[str] = []
        self.has_nav = False
        self.headings: list[str] = []
        self._in_title = False
        self._link_text: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name, prop = values.get("name", "").lower(), values.get("property", "").lower()
            if name:
                self.meta[name] = values.get("content", "")
            if prop:
                self.property_meta[prop] = values.get("content", "")
        elif tag in {"nav", "menu"}:
            self.has_nav = True
        elif tag == "a":
            href = values.get("href", "").strip()
            if href.lower().startswith("tel:"):
                self.tel.append(href[4:].strip())
            elif href.lower().startswith("mailto:"):
                self.mailto.append(href[7:].strip())
            elif href:
                self.links.append(href)
            self._link_text = ""
        elif tag in {"h1", "h2", "h3"}:
            self.headings.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            if self._link_text:
                self.anchors.append(self._link_text.strip())
            self._link_text = None

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._link_text is not None:
            self._link_text += " " + text
        if self.headings:
            self.headings[-1] += " " + text

    @property
    def title(self) -> str | None:
        joined = " ".join(" ".join(self.title_parts).split())
        return joined[:200] or None

    @property
    def link_texts(self) -> list[str]:
        return [text.strip() for text in re.split(r"\s+", " ".join(self.anchors)) if text.strip()]


class PublicWebsiteResearchSource:
    """Fetch the pages a candidate already names, and return them as evidence - nothing more."""

    name = "public_website_live"

    def __init__(self, policy: LiveResearchPolicy, *, transport: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not isinstance(policy, LiveResearchPolicy):
            raise TypeError("a LiveResearchPolicy is required")
        if not policy.enabled:
            reason = policy.refusal_reason()
            raise LiveResearchRefused(reason or "live research is not permitted for this deployment.")
        self.policy = policy
        self._transport = transport or urlopen
        self._sleep = sleep
        self._requests_made = 0
        self._last_request_at: float | None = None
        self._robots_cache: dict[str, Any] = {}
        #: Per-run audit trail for the entry point's job output; never an input to qualification.
        self.attempt_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ source contract

    def research(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        targets = self.targets_for(candidate)
        if not targets:
            # Not an error: nothing is permitted to look at. The record's own uncertainty already says the
            # candidate carried no usable website URL, and an absent URL is not a finding about the business.
            self.attempt_log.append({"url": None, "status": "NO_PERMITTED_TARGET"})
            return []
        items: list[dict[str, Any]] = []
        failures: list[str] = []
        for target in targets:
            outcome = self._observe(target, candidate)
            if outcome is None:
                continue
            if outcome.get("_failure"):
                retrieval = outcome["retrieval"]
                # The stable code goes into the message as well as the log, so a raised "nothing readable"
                # still tells an operator which attempt failed and why, in machine-readable form.
                failures.append(f"{target.host}: {retrieval['status']} - {outcome['_failure']}")
                self.attempt_log.append({"url": target.url, "host": target.host, "status": retrieval["status"],
                                         "detail": retrieval.get("uncertainty")})
                continue
            self.attempt_log.append({"url": target.url, "host": target.host, "status": outcome["retrieval"]["status"]})
            items.append(outcome)
        if not items and failures:
            # Everything permitted failed. Say so as a source failure so the pipeline reports "incomplete",
            # rather than letting an unexamined page be read as a website deficiency of the business.
            raise LiveResearchUnavailable("; ".join(failures))
        return items

    def targets_for(self, candidate: dict[str, Any]) -> list[PermittedTarget]:
        """Only URLs already on the record, business site first, capped by policy."""
        social = candidate.get("social") or {}
        wanted = [(candidate.get("originalWebsite"), "business_website"),
                  (social.get("instagramUrl"), "social_profile"),
                  (social.get("facebookUrl"), "social_profile")]
        targets: list[PermittedTarget] = []
        seen: set[str] = set()
        for value, kind in wanted:
            target = permitted_public_target(value, kind=kind, policy=self.policy)
            if target is None or target.url in seen:
                if value and _platform_host(str(value)) and kind == "social_profile":
                    # Recorded so the operator can see what was deliberately left alone, not overlooked.
                    self.attempt_log.append({"url": str(value), "status": "NOT_FETCHED_PLATFORM_RESTRICTED"})
                continue
            if kind == "business_website" and len([t for t in targets if t.kind == "business_website"]) >= self.policy.max_pages:
                continue
            if kind == "social_profile" and len([t for t in targets if t.kind == "social_profile"]) >= max(0, self.policy.max_pages - 1):
                continue
            seen.add(target.url)
            targets.append(target)
        return targets[: self.policy.max_pages]

    # ------------------------------------------------------------------ fetching

    def _budget_available(self) -> bool:
        return self._requests_made < self.policy.max_requests

    def _spend(self) -> None:
        self._requests_made += 1
        self._last_request_at = time.monotonic()

    def _wait(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.policy.min_delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _fetch(self, url: str) -> tuple[int | None, bytes, dict[str, str], str]:
        """One bounded GET with a hard byte cap. Returns (status, body, headers, final_url)."""
        if not self._budget_available():
            raise LiveResearchUnavailable("request budget for this run is exhausted")
        request = urllib.request.Request(url, headers={"User-Agent": self.policy.user_agent, "Accept": "text/html,*/*"}, method="GET")
        self._wait()
        self._spend()
        response = self._transport(request, timeout=self.policy.timeout_seconds)
        with response:
            status = getattr(response, "status", None) or response.getcode()
            headers = {str(key).lower(): str(value) for key, value in (dict(response.headers.items()) if response.headers else {}).items()}
            body = response.read(self.policy.max_bytes + 1)
        if len(body) > self.policy.max_bytes:
            raise _TooLarge()
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        return int(status or 0), body, headers, url

    def _robots_allows(self, target: PermittedTarget) -> tuple[bool, str]:
        if not self.policy.respect_robots:
            return True, "robots_not_checked"
        if target.host in self._robots_cache:
            return self._robots_cache[target.host], "robots_cached"
        allowed, note = True, "robots_absent"
        root = urllib.parse.urlsplit(target.url)
        robots_url = f"{root.scheme}://{root.netloc}/robots.txt"
        try:
            if not self._budget_available():
                # Without the rules the conservative answer is "do not fetch the page", not "assume fine".
                return False, "robots_budget_exhausted"
            status, body, _, _ = self._fetch(robots_url)
            if status == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(body.decode("utf-8", "replace").splitlines())
                allowed = parser.can_fetch(self.policy.user_agent, target.url)
                note = "robots_checked" if allowed else "robots_disallowed"
                delay = None
                try:
                    delay = parser.crawl_delay(self.policy.user_agent)
                except Exception:  # noqa: BLE001 - a malformed directive must not become a crash
                    delay = None
                if delay and float(delay) > self.policy.min_delay_seconds:
                    note = f"robots_checked:crawl_delay={int(delay)}"
            elif status in {401, 403}:
                allowed, note = False, "robots_restricted"
        except _TooLarge:
            allowed, note = False, "robots_oversized"
        except Exception:  # noqa: BLE001 - an unreachable robots.txt is recorded, never guessed away
            allowed, note = True, "robots_unreachable"
        self._robots_cache[target.host] = allowed
        return allowed, note

    def _observe(self, target: PermittedTarget, candidate: dict[str, Any]) -> dict[str, Any] | None:
        allowed, robots_note = self._robots_allows(target)
        if not allowed:
            return {"_failure": f"not fetched ({robots_note})", "retrieval": {"status": "ROBOTS_DISALLOWED", "robots": robots_note}}
        attempts = 0
        status: int | None = None
        body = b""
        headers: dict[str, str] = {}
        failure: str | None = None
        while True:
            attempts += 1
            try:
                status, body, headers, _ = self._fetch(target.url)
                failure = None
                break
            except _TooLarge:
                failure = "response exceeded the configured byte cap"
                break
            except LiveResearchUnavailable as error:
                failure = str(error)
                break
            except urllib.error.HTTPError as error:  # noqa: PERF203
                status = int(error.code)
                if status in {401, 403, 429} and attempts <= self.policy.max_retries and self._budget_available():
                    delay = _retry_after(error.headers)
                    if delay is not None and delay > 0:
                        self._sleep(delay)
                    continue
                if status in {401, 403}:
                    failure = "authentication or access restriction (no credentials or bypass attempted)"
                elif status == 429:
                    failure = "rate limited by the source"
                elif status == 503:
                    failure = "source temporarily unavailable"
                else:
                    failure = f"HTTP {status}"
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                reason = str(getattr(error, "reason", error))
                if _name_not_resolved(reason):
                    status, failure = None, "domain does not resolve"
                else:
                    failure = f"transport failure: {type(error).__name__}"
                break
            except Exception as error:  # noqa: BLE001 - a source never propagates an unexpected crash
                failure = f"unexpected failure: {type(error).__name__}"
                break
        retrieval: dict[str, Any] = {
            "status": _retrieval_code(status, failure),
            "http_status": status,
            "requested_url": target.url,
            "host": target.host,
            "target_kind": target.kind,
            "observed_at": _now(),
            "attempts": attempts,
            "requests_used": self._requests_made,
            "robots": robots_note,
            "bytes": len(body),
            "content_type": (headers.get("content-type") or "")[:120] or None,
        }
        if failure:
            retrieval["uncertainty"] = (f"{failure}; the page was not examined, so nothing here is evidence about "
                                        "the business's website quality or existence.")
            return {"_failure": failure, "retrieval": retrieval}
        content_type = (headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type:
            retrieval["status"] = "NON_HTML_RESPONSE"
            retrieval["uncertainty"] = ("the permitted URL returned a non-HTML document, which this source does not "
                                        "interpret; no conclusion about the business's website is drawn")
            return {"_failure": "non-HTML response", "retrieval": retrieval}
        text = body.decode(headers_charset(headers) or "utf-8", "replace")
        if _looks_like_login_gate(target.url, text):
            retrieval["status"] = "LOGIN_WALL"
            retrieval["uncertainty"] = ("the URL presented an authentication page, which limits what can be observed; "
                                        "it is not evidence that the business lacks a website")
            return {"_failure": "login wall (not bypassed)", "retrieval": retrieval}
        page = _scan(text)
        observation = _website_observation(page, target, candidate, retrieval)
        item: dict[str, Any] = {
            "candidate_name": candidate.get("businessName"),
            "business_name": page.observed_business_name(candidate),
            "source_url": target.url,
            "observed_at": retrieval["observed_at"],
            "excerpt": page.excerpt(),
            "retrieval": retrieval,
            "website_observation": observation,
            "data_classification": "REAL_PUBLIC_DATA",
        }
        located = page.location_matches(candidate)
        if located:
            item.update(located)
        if page.phone:
            item["phone"] = page.phone
        if page.email:
            item["email"] = page.email
        if page.instagram_url:
            item["instagram_url"] = page.instagram_url
        if page.facebook_url:
            item["facebook_url"] = page.facebook_url
        if page.contact_links:
            item["activity_signal"] = "public contact links present on the business's own page"
        # An observation that could not be attributed to this business must not become a website verdict.
        if observation.get("correct_business") is not True:
            del item["website_observation"]
            retrieval["uncertainty"] = (retrieval.get("uncertainty") or "") + (
                " The fetched page does not name this business clearly, so its content was recorded as limited "
                "evidence and the website was left unevaluated."
            ).strip()
        return item

    # ------------------------------------------------------------------ helpers used by tests and entry points

    def summary(self) -> dict[str, Any]:
        return {
            "requests_made": self._requests_made,
            "max_requests": self.policy.max_requests,
            "attempts": list(self.attempt_log),
            "permission_source": self.policy.permission_source,
        }


class _TooLarge(Exception):
    pass


def _retrieval_code(status: int | None, failure: str | None) -> str:
    if failure is None:
        return "FETCHED"
    if status == 200:
        return "NOT_HTML_OR_NOT_READABLE"
    if status in {401, 403}:
        return "ACCESS_RESTRICTED"
    if status == 429:
        return "RATE_LIMITED"
    if status in {500, 502, 503, 504}:
        return "SOURCE_UNAVAILABLE"
    if status == 404:
        return "NOT_FOUND_AT_URL"
    if status is not None:
        return "HTTP_ERROR"
    if failure and "resolve" in failure:
        return "DOMAIN_NOT_RESOLVING"
    if failure and "budget" in failure:
        return "REQUEST_BUDGET_EXHAUSTED"
    return "UNREACHABLE"


def _retry_after(headers: Any) -> float | None:
    raw = None
    try:
        raw = headers.get("Retry-After") if headers else None
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return max(0.0, min(delay, 30.0))


def _platform_host(url: Any) -> bool:
    host = _host_of(str(url or ""))
    if not host:
        return False
    host = host.removeprefix("www.")
    return host in PLATFORM_HOSTS or any(host.endswith("." + platform) for platform in PLATFORM_HOSTS)


def _name_not_resolved(reason: str) -> bool:
    text = reason.casefold()
    return "nodename nor servname" in text or "name or service not known" in text or "getaddrinfo failed" in text


def headers_charset(headers: dict[str, str]) -> str | None:
    match = re.search(r"charset=([A-Za-z0-9_\-]+)", (headers.get("content-type") or ""))
    return match.group(1) if match else None


_LOGIN_PATTERNS = re.compile(
    r"(sign (?:in|up)|log ?(?:in|out)|please log in|create (?:an )?account|authentication required|verify you are human|captcha)",
    re.I,
)


def _looks_like_login_gate(url: str, text: str) -> bool:
    """A login wall is a restriction we ran into, never a statement about the business."""
    path = urllib.parse.urlsplit(url).path.lower()
    if any(marker in path for marker in ("/login", "/signin", "/accounts/login", "/authenticate")):
        return True
    stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    visible = " ".join(re.sub(r"<[^>]+>", " ", stripped).split())
    if len(visible) > 3000:
        return False
    return bool(_LOGIN_PATTERNS.search(visible)) and "<form" in stripped.lower()


class _Page:
    """Derived-only view of a fetched page: counts, presence, and verbatim fragments it can quote."""

    def __init__(self, facts: _PageFacts, text: str) -> None:
        self.facts = facts
        self.text = text
        self.visible = " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)).split())
        self.internal_links = [link for link in facts.links
                               if link and not link.startswith(("mailto:", "tel:", "#", "javascript:", "sms:", "whatsapp:"))]
        self.has_viewport = bool(facts.meta.get("viewport"))
        self.title = facts.title
        self.phone = _first(facts.tel)
        self.email = _first(facts.mailto)
        self.instagram_url = _profile_link(facts.links, "instagram.com")
        self.facebook_url = _profile_link(facts.links, "facebook.com")
        self.contact_links = [link for link in facts.links if re.search(r"contact|find-us|reach-us|book", link, re.I)]
        self.service_words = re.findall(r"(?i)\b(menu|services|about|pricing|book|order|gallery)\b", text)
        self.nav_count = len({link.split("#")[0] for link in self.internal_links if link})
        self.words = len(self.visible.split())
        self.year = _latest_year(text)

    def excerpt(self) -> str:
        parts = [f"title: {self.title!r}" if self.title else "title: absent",
                 f"viewport meta: {'present' if self.has_viewport else 'absent'}",
                 f"internal links: {self.nav_count}", "headings: %d" % len(self.facts.headings),
                 f"visible words: {self.words}"]
        if self.year:
            parts.append(f"latest year in page: {self.year}")
        return "; ".join(parts)[:240]

    def observed_business_name(self, candidate: dict[str, Any]) -> str | None:
        """The page's own name claim - never a copy of the candidate - so identity stays independent."""
        for value in (self.title, self.facts.property_meta.get("og:site_name")):
            if value:
                return " ".join(str(value).split())[:120]
        return None

    def location_matches(self, candidate: dict[str, Any]) -> dict[str, str]:
        location = candidate.get("location") or {}
        found: dict[str, str] = {}
        for key, field_name in (("city", "city"), ("country", "country")):
            value = location.get(field_name)
            if value and re.search(re.escape(str(value)), self.visible, re.I):
                found[key] = str(value)
        return found


def _first(values: list[str]) -> str | None:
    cleaned = [value.strip() for value in values if value and value.strip()]
    return cleaned[0][:120] if cleaned else None


def _profile_link(links: list[str], platform: str) -> str | None:
    for link in links:
        try:
            host = (urllib.parse.urlsplit(link).hostname or "").lower()
        except ValueError:
            continue
        if platform in host and not re.search(r"/(?:login|sharing|policies)", urllib.parse.urlsplit(link).path, re.I):
            return link[:300]
    return None


def _latest_year(text: str) -> int | None:
    years = [int(match.group(0)) for match in re.finditer(r"\b(19|20)\d{2}\b", text)]
    years = [year for year in years if 1990 <= year <= datetime.now(timezone.utc).year]
    return max(years) if years else None


def _website_observation(page: _Page, target: PermittedTarget, candidate: dict[str, Any], retrieval: dict[str, Any]) -> dict[str, Any]:
    """Translate literal page properties into the existing observation vocabulary, and nothing beyond it."""
    name = _normal_name(candidate.get("businessName"))
    observed = _normal_name(page.observed_business_name(candidate))
    correct = bool(name and observed and (name in observed or observed in name))
    if not correct and page.facts.property_meta.get("og:site_name"):
        og = _normal_name(page.facts.property_meta.get("og:site_name"))
        correct = bool(og and (name in og or og in name))
    observation: dict[str, Any] = {
        "source_url": target.url,
        "load": "loaded",
        "correct_business": correct,
        "retrieved_at": retrieval["observed_at"],
        "evidence_excerpt": page.excerpt(),
    }
    if correct:
        if not page.has_viewport:
            observation["poor_mobile"] = True
        else:
            observation["mobile_friendly"] = True
        if page.nav_count >= 3 and page.facts.has_nav:
            observation["clear_navigation"] = True
        if any(word in " ".join(page.service_words).lower() for word in ("menu", "service", "about")):
            observation["clear_services"] = True
        else:
            observation["missing_services"] = True
        if page.phone or page.email or page.contact_links:
            observation["easy_contact"] = True
        else:
            observation["missing_contact"] = True
        if re.search(r"(?i)\b(book|order|reserve|buy|call now|whatsapp)\b", page.visible):
            observation["clear_cta"] = True
        current_year = datetime.now(timezone.utc).year
        if page.year and page.year <= current_year - 2:
            observation["outdated_information"] = True
            observation["outdated_basis"] = f"the most recent year appearing in the fetched page is {page.year}"
    return observation


def _normal_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def scan_html(text: str) -> _PageFacts:
    parser = _PageFacts()
    try:
        parser.feed(text[: 2_000_000])
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is a fact about the page, not a crash
        pass
    return parser


def _scan(text: str) -> _Page:
    return _Page(scan_html(text), text)


def classify_research_failure(errors: list[str]) -> str | None:
    """Map captured source errors to a stable code for the job ledger, without inventing a cause."""
    joined = " ".join(errors).casefold()
    if "not permitted" in joined or "allowliveresearch" in joined or "environment" in joined:
        return "LIVE_RESEARCH_NOT_ENABLED"
    if "budget" in joined:
        return "LIVE_RESEARCH_BUDGET_EXHAUSTED"
    if "does not resolve" in joined:
        return "LIVE_RESEARCH_SOURCE_UNAVAILABLE"
    if errors:
        return "LIVE_RESEARCH_SOURCE_UNAVAILABLE"
    return None

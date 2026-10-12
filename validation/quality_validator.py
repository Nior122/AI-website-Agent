"""Static, repeatable QA checks for local generated website artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urlparse


PLACEHOLDERS = ("lorem ipsum", "demo business", "example company", "your business name", "test restaurant", "test salon")
CLAIM_PATTERNS = (r"\b\d+\+?\s+(?:happy )?customers\b", r"\baward[- ]winning\b", r"\bnumber one\b", r"\bcertified\b", r"\btrusted by thousands\b", r"\bserving customers for \d+ years\b")
SECRET_PATTERNS = (r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", r"sk-[A-Za-z0-9]{12,}")


@dataclass
class Finding:
    severity: str
    category: str
    code: str
    message: str
    repair_suggestion: str | None = None


@dataclass
class ValidationResult:
    validation_id: str
    build_id: str | None
    lead_id: str | None
    status: str
    score: int
    started_at: str
    completed_at: str
    checks: dict[str, bool]
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["errors"] = [asdict(item) for item in self.findings if item.severity in {"CRITICAL", "ERROR"}]
        data["warnings"] = [asdict(item) for item in self.findings if item.severity == "WARNING"]
        return data


class _Inspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {key: value or "" for key, value in attrs}))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def validate_rendered_html(html: str, business_name: str, *, real_build: bool, build_id: str | None = None, lead_id: str | None = None, expected_contact: dict | None = None, synthetic_contact: bool = False) -> ValidationResult:
    started = _now()
    inspector, findings = _Inspector(), []
    try:
        inspector.feed(html)
        inspector.close()
    except Exception as error:
        findings.append(_finding("CRITICAL", "build_integrity", "HTML_PARSE_FAILURE", f"Generated HTML could not be parsed: {error}"))
    tags, page_text = inspector.tags, " ".join(inspector.text)
    ids = {attrs.get("id") for _, attrs in tags if attrs.get("id")}
    checks = {"build_integrity": bool(tags), "business_name": business_name in html, "semantic_landmarks": _has(tags, "header") and _has(tags, "main") and _has(tags, "footer"), "mobile_support": 'name="viewport"' in html and "@media" in html, "title": _has(tags, "title"), "meta_description": _meta(tags, "description"), "heading": _has(tags, "h1"), "links": True, "images": True, "contact_cta": True, "placeholder_scan": True, "claim_safety": True, "secret_scan": True, "performance": True}
    if not checks["build_integrity"]: findings.append(_finding("CRITICAL", "build_integrity", "EMPTY_OR_MISSING_OUTPUT", "No parseable HTML output was supplied."))
    if not checks["business_name"]: findings.append(_finding("ERROR", "content_integrity", "MISSING_BUSINESS_NAME", "Business name is absent from rendered output."))
    if not checks["semantic_landmarks"]: findings.append(_finding("ERROR", "accessibility", "MISSING_LANDMARKS", "Header, main, and footer landmarks are required."))
    if not checks["mobile_support"]: findings.append(_finding("ERROR", "responsiveness", "MISSING_MOBILE_SUPPORT", "Viewport metadata and responsive CSS are required."))
    if not checks["heading"]: findings.append(_finding("ERROR", "content_integrity", "MISSING_PRIMARY_HEADING", "A page needs an H1 heading."))
    if not checks["title"]: findings.append(_finding("WARNING", "seo", "MISSING_TITLE", "Page title is missing.", "Add a concise title using the verified business name."))
    if not checks["meta_description"]: findings.append(_finding("WARNING", "seo", "MISSING_META_DESCRIPTION", "Meta description is missing.", "Add a factual description grounded in verified business data."))
    _links(tags, ids, findings, checks); _images(tags, findings, checks, real_build); _contact(tags, {} if synthetic_contact else expected_contact or {}, findings, checks); _content_safety(page_text, findings, checks, real_build, raw_html=html); _performance(html, tags, findings, checks)
    return _result(build_id, lead_id, started, checks, findings)


def _links(tags, ids, findings, checks) -> None:
    for tag, attrs in tags:
        if tag != "a" or not attrs.get("href"): continue
        href = attrs["href"]
        if href.startswith("#") and href[1:] not in ids:
            checks["links"] = False; findings.append(_finding("ERROR", "links", "BROKEN_INTERNAL_LINK", f"Internal link {href} has no matching section."))
        elif href.startswith("https://wa.me/") and not re.fullmatch(r"https://wa\.me/\d+", href):
            checks["links"] = False; findings.append(_finding("ERROR", "links", "INVALID_WHATSAPP_LINK", "WhatsApp link is malformed."))
        elif href.startswith("tel:") and not re.fullmatch(r"tel:[+0-9 ()-]+", href):
            checks["links"] = False; findings.append(_finding("ERROR", "links", "INVALID_PHONE_LINK", "Phone link is malformed."))
        elif href.startswith(("http://", "https://")) and not urlparse(href).netloc:
            checks["links"] = False; findings.append(_finding("ERROR", "links", "INVALID_EXTERNAL_LINK", "External link is malformed."))
        elif not href.startswith(("#", "http://", "https://", "tel:")):
            checks["links"] = False; findings.append(_finding("ERROR", "links", "UNSUPPORTED_LINK_SCHEME", f"Unsupported link scheme: {href}."))


def _images(tags, findings, checks, real_build) -> None:
    for tag, attrs in tags:
        if tag != "img": continue
        src, alt = attrs.get("src"), attrs.get("alt")
        if not src or not urlparse(src).scheme or not urlparse(src).netloc:
            checks["images"] = False; findings.append(_finding("ERROR", "images", "INVALID_IMAGE_SOURCE", "Image source is not a valid absolute URL."))
        if not alt:
            checks["images"] = False; findings.append(_finding("ERROR", "accessibility", "MISSING_IMAGE_ALT", "Image is missing alt text.", "Provide a factual alt description or omit the image."))
        if real_build and "example.invalid" in (src or ""):
            checks["images"] = False; findings.append(_finding("ERROR", "images", "DEMO_IMAGE_IN_REAL_BUILD", "A demo image URL appears in a real build."))


def _contact(tags, expected, findings, checks) -> None:
    hrefs = {attrs.get("href") for tag, attrs in tags if tag == "a"}
    if expected.get("whatsapp") and not any(href and href.startswith("https://wa.me/") for href in hrefs):
        checks["contact_cta"] = False; findings.append(_finding("WARNING", "contact_cta", "MISSING_WHATSAPP_CTA", "Verified WhatsApp is available but no WhatsApp CTA rendered.", "Render a WhatsApp CTA or document why it is omitted."))
    if expected.get("phone") and not any(href and href.startswith("tel:") for href in hrefs):
        checks["contact_cta"] = False; findings.append(_finding("WARNING", "contact_cta", "MISSING_PHONE_CTA", "Verified phone is available but no call CTA rendered."))


def _content_safety(text, findings, checks, real_build, raw_html="") -> None:
    lowered = text.casefold()
    if real_build:
        for pattern in PLACEHOLDERS:
            if pattern in lowered:
                checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "placeholder", "PLACEHOLDER_CONTENT", f"Suspicious placeholder found: {pattern}."))
        for spam_word in ("casino", "gambling", "non gamstop", "jackpot", "roulette"):
            if re.search(rf"\b{spam_word}\b", lowered):
                checks["claim_safety"] = False; findings.append(_finding("ERROR", "content_integrity", "SPAM_CONTENT_DETECTED", f"Suspicious spam keyword detected: {spam_word}."))
        if re.search(r"\[\/?(?:vc_|rodich_|[a-z0-9_-]+_row|[a-z0-9_-]+_column)", raw_html, re.IGNORECASE):
            checks["build_integrity"] = False; findings.append(_finding("ERROR", "content_integrity", "UNPARSED_SHORTCODE_DETECTED", "Unparsed builder shortcode markup found in rendered HTML."))
        if re.search(r"(?:left|top|margin-left|margin-top)\s*:\s*-\s*[0-9]{4,}px", raw_html, re.IGNORECASE):
            checks["build_integrity"] = False; findings.append(_finding("ERROR", "security", "HIDDEN_OFFSCREEN_SPAM_DETECTED", "Hidden off-screen positioning detected in HTML styles."))
    for pattern in CLAIM_PATTERNS:
        if re.search(pattern, lowered):
            checks["claim_safety"] = False; findings.append(_finding("WARNING", "business_claim_safety", "UNSUPPORTED_CLAIM", "Potentially unsupported business claim requires evidence review."))
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            checks["secret_scan"] = False; findings.append(_finding("CRITICAL", "security", "POTENTIAL_SECRET", "Potential secret-like value detected and redacted from this report."))


def _performance(html, tags, findings, checks) -> None:
    if len(html.encode("utf-8")) > 500_000:
        checks["performance"] = False; findings.append(_finding("WARNING", "performance", "LARGE_HTML_ARTIFACT", "HTML exceeds 500 KB; review payload size."))


def _result(build_id, lead_id, started, checks, findings) -> ValidationResult:
    score = max(0, 100 - sum({"CRITICAL": 100, "ERROR": 20, "WARNING": 5, "INFO": 0}[item.severity] for item in findings))
    severities = {item.severity for item in findings}
    status = "FAIL" if severities & {"CRITICAL", "ERROR"} else "NEEDS_REVIEW" if "WARNING" in severities else "PASS"
    completed = _now()
    ident = f"validation-{build_id.removeprefix('build-')}" if build_id else f"validation-{completed.replace(':', '').replace('Z', 'z')}"
    return ValidationResult(ident, build_id, lead_id, status, score, started, completed, checks, findings)


def _finding(severity, category, code, message, repair_suggestion=None): return Finding(severity, category, code, message, repair_suggestion)
def _has(tags, wanted): return any(tag == wanted for tag, _ in tags)
def _meta(tags, wanted): return any(tag == "meta" and attrs.get("name") == wanted and attrs.get("content") for tag, attrs in tags)
def _now() -> str: return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

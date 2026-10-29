"""Static, repeatable QA checks for local generated website artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urlparse


PLACEHOLDERS = ("lorem ipsum", "demo business", "example company", "your business name", "test restaurant", "test salon")
FICTIONAL_DEMO_PATTERNS = (
    "lagos fresh kitchen",
    "the lagos kitchen",
    "demo riverside kitchen",
    "riverside kitchen",
    "fictional harbour kitchen",
    "fictional glow studio",
    "fictional advisory practice",
    "demo glow salon",
    "fictional test business",
    "signature rice bowl",
    "grilled plantain plate",
    "lagos jollof plate",
    "grilled fish & plantain",
    "golden plantain bites",
    "jollof rice test bowl",
    "grilled fish test plate",
    "plantain chips test snack",
    "fictional test kitchen street",
    "fictional waterfront address",
    "from the test kitchen",
    "fictional dishes for layout testing",
    "fictional test schedule",
    "a fictional setting",
    "find the test kitchen",
    "this address is fictional",
    "a considered journey, safely contained",
    "this fictional concept intentionally has no live phone",
    "fictional demonstration website",
    "lagos table · fictional test kitchen",
    "made for the table",
    "made for your table",
    "decorative illustration of a shared meal",
    "+234 800 000 0099",
    "+234 800 000 0002",
    "+234 800 000 0001",
    "example.invalid",
    "demo price",
    "₦0 demo",
    "[test]",
)
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
        self.body_text: list[str] = []
        self.h1_text: list[str] = []
        self._in_title = False
        self._in_style_or_script = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        self.tags.append((lower, {key: value or "" for key, value in attrs}))
        if lower == "title":
            self._in_title = True
        elif lower in {"style", "script"}:
            self._in_style_or_script = True
        elif lower == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "title":
            self._in_title = False
        elif lower in {"style", "script"}:
            self._in_style_or_script = False
        elif lower == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if not self._in_title and not self._in_style_or_script:
            self.body_text.append(data)
        if self._in_h1:
            self.h1_text.append(data)


def validate_rendered_html(html: str, business_name: str, *, real_build: bool, build_id: str | None = None, lead_id: str | None = None, expected_contact: dict | None = None, expected_facts: dict | None = None, synthetic_contact: bool = False) -> ValidationResult:
    started = _now()
    inspector, findings = _Inspector(), []
    try:
        inspector.feed(html)
        inspector.close()
    except Exception as error:
        findings.append(_finding("CRITICAL", "build_integrity", "HTML_PARSE_FAILURE", f"Generated HTML could not be parsed: {error}"))
    tags, page_text, body_page_text = inspector.tags, " ".join(inspector.text), " ".join(inspector.body_text)
    ids = {attrs.get("id") for _, attrs in tags if attrs.get("id")}
    checks = {"build_integrity": bool(tags), "business_name": bool(business_name) and business_name in body_page_text, "semantic_landmarks": _has(tags, "header") and _has(tags, "main") and _has(tags, "footer"), "mobile_support": 'name="viewport"' in html and "@media" in html, "title": _has(tags, "title"), "meta_description": _meta(tags, "description"), "heading": _has(tags, "h1"), "links": True, "images": True, "contact_cta": True, "placeholder_scan": True, "claim_safety": True, "secret_scan": True, "performance": True}
    if not checks["build_integrity"]: findings.append(_finding("CRITICAL", "build_integrity", "EMPTY_OR_MISSING_OUTPUT", "No parseable HTML output was supplied."))
    if not checks["business_name"]: findings.append(_finding("ERROR", "content_integrity", "MISSING_BUSINESS_NAME", "Business name is absent from rendered page body content."))
    if not checks["semantic_landmarks"]: findings.append(_finding("ERROR", "accessibility", "MISSING_LANDMARKS", "Header, main, and footer landmarks are required."))
    if not checks["mobile_support"]: findings.append(_finding("ERROR", "responsiveness", "MISSING_MOBILE_SUPPORT", "Viewport metadata and responsive CSS are required."))
    if not checks["heading"]: findings.append(_finding("ERROR", "content_integrity", "MISSING_PRIMARY_HEADING", "A page needs an H1 heading."))
    if not checks["title"]: findings.append(_finding("WARNING", "seo", "MISSING_TITLE", "Page title is missing.", "Add a concise title using the verified business name."))
    if not checks["meta_description"]: findings.append(_finding("WARNING", "seo", "MISSING_META_DESCRIPTION", "Meta description is missing.", "Add a factual description grounded in verified business data."))
    _links(tags, ids, findings, checks); _images(tags, findings, checks, real_build); _contact(tags, {} if synthetic_contact else expected_contact or {}, findings, checks, body_page_text, real_build=real_build); _verify_expected_facts(tags, ids, {} if synthetic_contact else expected_facts or {}, findings, checks, body_page_text, real_build=real_build); _content_safety(page_text, findings, checks, real_build, raw_html=html, synthetic_contact=synthetic_contact); _performance(html, tags, findings, checks)
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


def _contact(tags, expected, findings, checks, page_text="", real_build=False) -> None:
    hrefs = {attrs.get("href") for tag, attrs in tags if tag == "a" and attrs.get("href")}
    if expected.get("whatsapp"):
        wa_links = [href for href in hrefs if href.startswith("https://wa.me/")]
        if not wa_links:
            checks["contact_cta"] = False; findings.append(_finding("WARNING", "contact_cta", "MISSING_WHATSAPP_CTA", "Verified WhatsApp is available but no WhatsApp CTA rendered.", "Render a WhatsApp CTA or document why it is omitted."))
        else:
            expected_digits = re.sub(r"\D", "", str(expected["whatsapp"]))
            if expected_digits and not any(re.sub(r"\D", "", href) == expected_digits for href in wa_links):
                checks["contact_cta"] = False; findings.append(_finding("ERROR", "contact_cta", "MISMATCHED_WHATSAPP_CTA", "Rendered WhatsApp CTA does not match verified WhatsApp number."))
    if expected.get("phone"):
        tel_links = [href for href in hrefs if href.startswith("tel:")]
        if not tel_links:
            checks["contact_cta"] = False; findings.append(_finding("WARNING", "contact_cta", "MISSING_PHONE_CTA", "Verified phone is available but no call CTA rendered."))
        else:
            expected_digits = re.sub(r"\D", "", str(expected["phone"]))
            if expected_digits and not any(re.sub(r"\D", "", href) == expected_digits for href in tel_links):
                checks["contact_cta"] = False; findings.append(_finding("ERROR", "contact_cta", "MISMATCHED_PHONE_CTA", "Rendered phone CTA does not match verified phone number."))
    if expected.get("email") and expected["email"].lower() not in page_text.lower():
        checks["contact_cta"] = False; findings.append(_finding("ERROR" if real_build else "WARNING", "contact_cta", "MISSING_VERIFIED_EMAIL", f"Verified email {expected['email']} is missing from page."))


def _verify_expected_facts(tags, ids, expected_facts, findings, checks, page_text="", real_build=False) -> None:
    if not expected_facts:
        return
    lowered = page_text.casefold()
    hrefs = {attrs.get("href") for tag, attrs in tags if tag == "a" and attrs.get("href")}
    address = expected_facts.get("address")
    if address and str(address).casefold() not in lowered:
        checks["business_name"] = False; findings.append(_finding("ERROR", "content_integrity", "MISSING_VERIFIED_ADDRESS", "Verified business address is missing from rendered page content."))
    description = expected_facts.get("description")
    if description and str(description).casefold() not in lowered:
        checks["business_name"] = False; findings.append(_finding("ERROR", "content_integrity", "MISSING_VERIFIED_DESCRIPTION", "Verified business description is missing from rendered page content."))
    services = expected_facts.get("services") or []
    for item in services:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name and name.casefold() not in lowered:
            checks["business_name"] = False; findings.append(_finding("ERROR", "content_integrity", "MISSING_VERIFIED_OFFERING", f"Verified offering '{name}' is missing from rendered page content."))
    if real_build and services and not expected_facts.get("menu_items") and expected_facts.get("category") in {"restaurant", "food_business"}:
        if "menu" in ids or "signature dishes" in lowered:
            checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "content_integrity", "MISLABELED_SERVICES_AS_MENU", "Verified services were mislabeled as food menu items when no food menu is verified."))
    if "opening_hours" in expected_facts:
        hours_val = expected_facts.get("opening_hours")
        if hours_val and str(hours_val).casefold() not in lowered:
            checks["business_name"] = False; findings.append(_finding("ERROR", "content_integrity", "MISSING_VERIFIED_HOURS", "Verified opening hours are missing from rendered page content."))
        elif not hours_val and real_build and ("hours" in ids or "opening hours" in lowered):
            checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "content_integrity", "UNVERIFIED_HOURS_RENDERED", "Opening hours section was rendered despite opening hours being unverified."))
    instagram = expected_facts.get("instagram_url")
    if instagram and instagram not in hrefs:
        checks["contact_cta"] = False; findings.append(_finding("ERROR", "contact_cta", "MISSING_VERIFIED_INSTAGRAM", "Verified Instagram link is missing from rendered page."))
    facebook = expected_facts.get("facebook_url")
    if facebook and facebook not in hrefs:
        checks["contact_cta"] = False; findings.append(_finding("ERROR", "contact_cta", "MISSING_VERIFIED_FACEBOOK", "Verified Facebook link is missing from rendered page."))
    disputed_facts = expected_facts.get("disputed_facts") or {}
    if real_build and isinstance(disputed_facts, dict):
        hrefs_lower = " ".join(str(h) for h in hrefs).casefold()
        href_digits = {re.sub(r"\D", "", str(h)) for h in hrefs if str(h).startswith(("tel:", "https://wa.me/"))}
        for field_name, info in disputed_facts.items():
            values = info.get("values") if isinstance(info, dict) else (info if isinstance(info, list) else [])
            for val in values or []:
                val_str = str(val or "").strip()
                if not val_str:
                    continue
                val_digits = re.sub(r"\D", "", val_str) if field_name in {"phone", "whatsapp"} else ""
                if (
                    val_str.casefold() in lowered
                    or val_str.casefold() in hrefs_lower
                    or (len(val_digits) >= 7 and val_digits in href_digits)
                ):
                    checks["claim_safety"] = False
                    findings.append(_finding(
                        "ERROR",
                        "content_integrity",
                        "DISPUTED_FACT_RENDERED",
                        f"Disputed or conflicting fact for '{field_name}' ('{val_str}') was rendered as confirmed content.",
                    ))


def _content_safety(text, findings, checks, real_build, raw_html="", synthetic_contact=False) -> None:
    lowered = text.casefold()
    raw_lower = raw_html.casefold()
    if real_build:
        for pattern in PLACEHOLDERS:
            if pattern in lowered:
                checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "placeholder", "PLACEHOLDER_CONTENT", f"Suspicious placeholder found: {pattern}."))
        if not synthetic_contact:
            for pattern in FICTIONAL_DEMO_PATTERNS:
                if pattern in lowered or pattern in raw_lower:
                    checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "content_integrity", "FICTIONAL_DEMO_DATA_IN_REAL_BUILD", f"Fictional demo artifact found in real build: {pattern}."))
            if re.search(r'<span class="brand-mark"[^>]*>\s*LF\s*</span>', raw_html):
                checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "content_integrity", "FICTIONAL_BRAND_MARK_IN_REAL_BUILD", "Fictional brand mark 'LF' found in real build."))
            if 'class="hero-art"' in raw_lower or 'class="fictional-disclosure"' in raw_lower:
                checks["placeholder_scan"] = False; findings.append(_finding("ERROR", "content_integrity", "FICTIONAL_COMPONENT_IN_REAL_BUILD", "Fictional template component or disclosure found in real build."))
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

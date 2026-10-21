"""Opt-in Tavily search enrichment for existing Scout candidates.

This module uses search results as evidence. It does not contact businesses,
send messages, or treat an unverified website candidate as a verified site.
"""

from __future__ import annotations

from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import json
import re


TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_RESULTS = 5

_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

_PHONE_PATTERN = re.compile(
    r"(?<!\w)\+?\d[\d\s()./-]{6,}\d(?!\w)"
)

_SOCIAL_HOSTS = {
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "linkedin.com",
    "youtube.com",
    "x.com",
    "twitter.com",
}

_DIRECTORY_HOSTS = {
    "google.com",
    "yelp.com",
    "tripadvisor.com",
    "zomato.com",
    "foursquare.com",
    "restaurantguru.com",
    "yellowpages.com",
    "hotfrog.com",
}


def _normalise(value: Any) -> str:
    """Lowercase text and remove punctuation and whitespace."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _valid_url(value: Any) -> str | None:
    """Accept only absolute HTTP or HTTPS URLs."""
    if not isinstance(value, str):
        return None

    value = value.strip()
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    return value


def _host(value: str) -> str:
    parsed = urlparse(value)
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _host_matches(host: str, known_host: str) -> bool:
    return host == known_host or host.endswith("." + known_host)


def _add_unique(items: list, value: Any) -> None:
    if value is not None and value not in items:
        items.append(value)


def _record_field_source(
    lead: dict[str, Any],
    field_name: str,
    source_url: str,
) -> None:
    field_sources = lead.setdefault("fieldSources", {})
    urls = field_sources.setdefault(field_name, [])
    _add_unique(urls, source_url)


class TavilySearchEnricher:
    """Search for public evidence about an existing candidate.

    One call performs one search. The caller is responsible for requiring
    explicit operator approval before creating this adapter.
    """

    name = "tavily_search"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int = 20,
        max_results: int = MAX_RESULTS,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A Tavily API key is required.")

        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60.")

        if not 1 <= max_results <= MAX_RESULTS:
            raise ValueError(f"max_results must be between 1 and {MAX_RESULTS}.")

        self._api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results

    def _search(self, query: str) -> list[dict[str, Any]]:
        payload = {
            "query": query,
            "topic": "general",
            "search_depth": "basic",
            "max_results": self.max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        request = Request(
            TAVILY_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AI-Website-Agent/0.1",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(
                f"Tavily search failed with HTTP status {error.code}."
            ) from None
        except URLError:
            raise RuntimeError("Could not connect to the Tavily search API.") from None
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError(
                "Tavily search timed out or returned invalid JSON."
            ) from None

        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RuntimeError("Tavily returned an unexpected response format.")

        results: list[dict[str, Any]] = []

        for item in data["results"][: self.max_results]:
            if not isinstance(item, dict):
                continue

            url = _valid_url(item.get("url"))
            if not url:
                continue

            title = item.get("title")
            content = item.get("content")
            score = item.get("score")

            results.append(
                {
                    "title": title[:300] if isinstance(title, str) else "",
                    "url": url,
                    "content": content[:1200] if isinstance(content, str) else "",
                    "score": score if isinstance(score, (int, float)) else None,
                }
            )

        return results

    def enrich(self, lead: dict[str, Any], target: Any) -> None:
        """Add source-backed search findings to a normalized Scout lead.

        Search-result matches remain provisional. A human must verify the
        business identity and its official links before relying on them.
        """
        business_name = lead.get("businessName")
        if not isinstance(business_name, str) or not business_name.strip():
            return

        location = lead.get("location", {})
        address = location.get("address") or ""
        city = getattr(target, "city", None) or location.get("city") or ""
        country = (
            getattr(target, "country", None)
            or location.get("country")
            or ""
        )
        niche = getattr(target, "niche", None) or lead.get("niche") or ""

        query_parts = [
            f'"{business_name.strip()}"',
            str(address).strip(),
            str(city).strip(),
            str(country).strip(),
            str(niche).strip(),
            "official website contact phone email Instagram Facebook",
        ]
        query = " ".join(part for part in query_parts if part)

        results = self._search(query)

        business_key = _normalise(business_name)
        evidence_items = lead.setdefault("researchEvidence", [])
        source_urls = lead.setdefault("sourceUrls", [])
        discovery_sources = lead.setdefault("discoverySources", [])
        notes = lead.setdefault("notes", [])
        contact = lead.setdefault("contact", {})
        social = lead.setdefault("social", {})
        public_info = lead.setdefault("publicBusinessInformation", {})

        for result in results:
            title = result["title"]
            content = result["content"]
            result_url = result["url"]
            combined_text = _normalise(f"{title} {content}")
            matched = bool(business_key and business_key in combined_text)

            evidence = {
                "sourceUrl": result_url,
                "title": title,
                "snippet": content,
                "score": result["score"],
                "businessNameMatched": matched,
            }
            evidence_items.append(evidence)

            # Don't attach contacts or websites from a result that does not
            # mention the requested business name.
            if not matched:
                continue

            _add_unique(source_urls, result_url)
            _add_unique(discovery_sources, self.name)

            host = _host(result_url)
            parsed = urlparse(result_url)
            path = parsed.path.strip("/")
            searchable_text = f"{title} {content}"

            # Social links are only candidates. Their association with the
            # business still needs manual verification.
            if _host_matches(host, "instagram.com") and path:
                if not social.get("instagramUrl"):
                    social["instagramUrl"] = result_url
                    _record_field_source(
                        lead, "social.instagramUrl", result_url
                    )

            elif _host_matches(host, "facebook.com") and path:
                if not social.get("facebookUrl"):
                    social["facebookUrl"] = result_url
                    _record_field_source(
                        lead, "social.facebookUrl", result_url
                    )

            elif any(_host_matches(host, item) for item in _SOCIAL_HOSTS):
                other_urls = social.setdefault("otherSocialUrls", [])
                _add_unique(other_urls, result_url)
                _record_field_source(
                    lead, "social.otherSocialUrls", result_url
                )

            # A non-directory web result may be a website candidate, but
            # this search alone cannot prove ownership or site quality.
            elif not any(
                _host_matches(host, item) for item in _DIRECTORY_HOSTS
            ):
                if not lead.get("originalWebsite"):
                    lead["originalWebsite"] = result_url

                    if lead.get("websiteStatus") in {"none_known", "unknown"}:
                        lead["websiteStatus"] = "unknown"

                    _record_field_source(
                        lead, "originalWebsite", result_url
                    )
                    _add_unique(
                        notes,
                        "Unverified possible website found through Tavily "
                        f"search: {result_url}. Verify ownership and site "
                        "quality before making a claim about this business.",
                    )

            # Extract an email only from a result that matched the business.
            email_match = _EMAIL_PATTERN.search(searchable_text)
            if email_match and not contact.get("email"):
                contact["email"] = email_match.group(0)
                _record_field_source(
                    lead, "contact.email", result_url
                )

            # Extract a plausible phone number from matching search content.
            if not contact.get("phone"):
                for phone_match in _PHONE_PATTERN.finditer(searchable_text):
                    phone = phone_match.group(0).strip()
                    digit_count = sum(character.isdigit() for character in phone)

                    if 8 <= digit_count <= 15:
                        contact["phone"] = phone
                        _record_field_source(
                            lead, "contact.phone", result_url
                        )
                        break

            # Record service information only when the candidate doesn't
            # already have a description. Never overwrite source data.
            if not public_info.get("description") and title:
                public_info["description"] = title
                _record_field_source(
                    lead, "publicBusinessInformation.description", result_url
                )
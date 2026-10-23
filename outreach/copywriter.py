"""AI email copywriter adapter. Draft-only; human approval and send gates remain separate."""
from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from integrations.http import ProviderError, provider_request

class CopywriterError(ValueError):
    def __init__(self, message: str, *, code: str = "COPYWRITER_FAILED"):
        super().__init__(message)
        self.code = code

class MarketingEmailWriter:
    """Grounded, schema-checked email drafting with explicit provider fallback."""
    def __init__(self, *, request=provider_request, production=None):
        self.request = request
        if production is None:
            from tracking.database import is_production_mode
            production = is_production_mode()
        self.production = bool(production)

    def generate(self, lead, preview_url, evidence):
        business = str(lead.get("business_name") or "").strip()
        parsed = urlsplit(str(preview_url or ""))
        if not business or parsed.scheme != "https" or not parsed.hostname:
            raise CopywriterError("Verified business name and HTTPS preview URL are required.", code="COPYWRITER_INPUT_INVALID")
        facts = []
        for item in evidence or []:
            value = str(item or "").strip()
            if value and "@" not in value and not re.search(r"(?i)(token|password|secret|api[_ -]?key)\s*[:=]", value):
                facts.append(value[:300])
            if len(facts) >= 8:
                break
        if not facts and lead.get("business_type"):
            facts = ["Recorded business category: " + str(lead["business_type"])[:120]]
        if not facts:
            raise CopywriterError("Verified business facts are required.", code="COPYWRITER_FACTS_MISSING")
        order = [x.strip().lower() for x in os.environ.get("AI_WEBSITE_AGENT_EMAIL_COPYWRITER_PROVIDERS", "openrouter,nvidia").split(",") if x.strip()]
        if not order or any(x not in {"openrouter", "nvidia"} for x in order):
            raise CopywriterError("Provider order must contain only openrouter and/or nvidia.", code="COPYWRITER_CONFIG_INVALID")
        failures = []
        for provider in order:
            try:
                result = self._call(provider, business, str(lead.get("business_type") or ""), str(preview_url), facts)
                subject, body = str(result.get("subject") or "").strip(), str(result.get("body") or "").strip()
                if not subject or len(subject) > 120 or not body or len(body) > 2400:
                    raise CopywriterError("Subject/body length is invalid.", code="COPYWRITER_OUTPUT_INVALID")
                if business.casefold() not in body.casefold() or preview_url not in body:
                    raise CopywriterError("Draft must name the correct business and include the exact preview URL.", code="COPYWRITER_GROUNDING_FAILED")
                urls = re.findall(r"https?://[^\s<>()]+", body)
                if any(x.rstrip(".,;") != preview_url for x in urls):
                    raise CopywriterError("Draft contains an unapproved URL.", code="COPYWRITER_URL_NOT_ALLOWED")
                if any(x in body.casefold() for x in ("award-winning", "guaranteed", "number one", "certified", "years in business")):
                    raise CopywriterError("Draft contains an unsupported claim.", code="COPYWRITER_UNSUPPORTED_CLAIM")
                return {"subject": subject, "body": body, "metadata": {"provider": provider,
                        "model": result.get("_model"), "route": result.get("_route"),
                        "prompt_version": "marketing-email-v1",
                        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}}
            except (CopywriterError, ProviderError) as error:
                failures.append(provider + ":" + getattr(error, "code", "PROVIDER_ERROR"))
        raise CopywriterError("All eligible AI providers failed (" + ", ".join(failures) + "). No draft was saved.",
                              code="COPYWRITER_PROVIDERS_UNAVAILABLE")

    def _call(self, provider, business, category, url, facts):
        if provider == "openrouter":
            key = (os.environ.get("AI_WEBSITE_AGENT_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
            model = (os.environ.get("AI_WEBSITE_AGENT_OPENROUTER_EMAIL_MODEL") or "openrouter/free").strip()
            if model != "openrouter/free" and not model.endswith(":free"):
                raise CopywriterError("Paid OpenRouter model refused; use openrouter/free or a :free model.", code="PAID_MODEL_REFUSED")
            if not key:
                raise CopywriterError("OpenRouter API key is missing.", code="OPENROUTER_KEY_MISSING")
            endpoint = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": "Bearer " + key,
                       "HTTP-Referer": os.environ.get("AI_WEBSITE_AGENT_SITE_URL", "https://josephportifolio.vercel.app"),
                       "X-Title": "AI Website Agent"}
            route = "openrouter-free-router-best-effort"
        else:
            key = (os.environ.get("AI_WEBSITE_AGENT_NVIDIA_API_KEY") or os.environ.get("NVIDIA_API_KEY") or "").strip()
            model = (os.environ.get("AI_WEBSITE_AGENT_NVIDIA_EMAIL_MODEL") or "").strip()
            if not key or not model:
                raise CopywriterError("NVIDIA API key and model must be configured.", code="NVIDIA_CONFIG_MISSING")
            if self.production and os.environ.get("AI_WEBSITE_AGENT_NVIDIA_PRODUCTION_LICENSE_CONFIRMED") != "1":
                raise CopywriterError("NVIDIA Developer API is disabled in production until its production license is confirmed.", code="NVIDIA_PRODUCTION_LICENSE_REQUIRED")
            endpoint = (os.environ.get("AI_WEBSITE_AGENT_NVIDIA_CHAT_COMPLETIONS_URL") or "https://integrate.api.nvidia.com/v1/chat/completions").strip()
            parts = urlsplit(endpoint)
            if parts.scheme != "https" or parts.hostname != "integrate.api.nvidia.com":
                raise CopywriterError("NVIDIA endpoint must be the official HTTPS host.", code="NVIDIA_ENDPOINT_REFUSED")
            headers = {"Authorization": "Bearer " + key}
            route = "nvidia-licensed-endpoint"
        prompt = ("Write a concise, respectful first-contact B2B website-preview email (90-140 words). Draft only. "
                  "Use only the verified facts; do not invent defects, awards, years, customers, revenue, traffic, "
                  "certifications, prior contact, or promises. No Markdown, HTML, or fabricated signature. Include "
                  "the exact preview URL and invite a low-pressure review. Return JSON with subject and body strings.\n"
                  + "Business: " + business + "\nCategory: " + category + "\nVerified facts:\n- "
                  + "\n- ".join(facts) + "\nExact preview URL: " + url)
        response = self.request(endpoint, method="POST", json_body={"model": model, "messages": [
            {"role": "system", "content": "Return only a valid JSON object with subject and body strings."},
            {"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": 650,
            "response_format": {"type": "json_object"}}, headers={**headers, "Content-Type": "application/json"},
            timeout=30.0, max_bytes=100000)
        payload = response.get("json") if isinstance(response, dict) else None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        try:
            result = json.loads(message.get("content") or "") if isinstance(message, dict) else None
        except (json.JSONDecodeError, TypeError):
            result = None
        if not isinstance(result, dict) or not isinstance(result.get("subject"), str) or not isinstance(result.get("body"), str):
            raise CopywriterError("AI response did not contain valid subject/body JSON.", code="COPYWRITER_OUTPUT_INVALID")
        result["_model"] = str(payload.get("model") or model)
        result["_route"] = route
        return result

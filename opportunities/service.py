"""Turn source-backed Scout candidates or Research records into safe, reviewable opportunity briefs."""
from __future__ import annotations
from datetime import datetime, timezone

from research.research_agent import is_fixture_or_doc_source
from templates.registry import select_template

def _now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

WEBSITE_MAP={
    "none_known":"WEBSITE_UNKNOWN",
    "unknown":"WEBSITE_UNKNOWN",
    "poor":"WEBSITE_POOR",
    "outdated":"WEBSITE_OUTDATED",
    "basic":"WEBSITE_BASIC",
    "professional":"WEBSITE_MODERN",
    "NO_WEBSITE":"NO_WEBSITE",
    "WEBSITE_UNKNOWN":"WEBSITE_UNKNOWN",
    "WEBSITE_POOR":"WEBSITE_POOR",
    "WEBSITE_OUTDATED":"WEBSITE_OUTDATED",
    "WEBSITE_BASIC":"WEBSITE_BASIC",
    "WEBSITE_MODERN":"WEBSITE_MODERN",
}

def assess_opportunity(candidate, *, research=None, lead_id=None, worker_id=None, job_id=None):
    """Return a qualification record and Builder handoff without asserting missing facts.

    Accepts either a Scout candidate dict or a full ResearchAgent record
    (``{"candidate": ..., "research": ...}``). A directory/listing lacking a
    website field is insufficient evidence for ``NO_WEBSITE``; it becomes
    ``WEBSITE_UNKNOWN`` and requires review.
    """
    top_classification = None
    if isinstance(candidate, dict) and isinstance(candidate.get("candidate"), dict) and isinstance(candidate.get("research"), dict):
        top_classification = candidate.get("data_classification")
        research = research or candidate.get("research")
        lead_id = lead_id or candidate.get("lead_id")
        candidate = candidate["candidate"]
    research = research if isinstance(research, dict) else {}
    classification = top_classification or candidate.get("dataClassification", "TEST_FIXTURE")
    is_real = classification == "REAL_PUBLIC_DATA"

    raw_sources = list(dict.fromkeys([
        *(candidate.get("sourceUrls") or []),
        *(research.get("researchSources") or []),
    ]))
    source_urls = [u for u in raw_sources if not (is_real and research and is_fixture_or_doc_source(u))]

    conflicting_items = [
        *(research.get("conflictingFacts") or []),
        *(research.get("disputedFacts") or []),
    ]
    conflicting_fields = {
        {"instagramUrl": "instagram_url", "facebookUrl": "facebook_url", "businessName": "business_name"}.get(
            str(item.get("field")), str(item.get("field"))
        )
        for item in conflicting_items
        if isinstance(item, dict) and item.get("field")
    }

    verified_map = {}
    for item in research.get("verifiedFacts") or []:
        if isinstance(item, dict) and item.get("field") and item.get("value") not in (None, "", [], {}):
            verified_map[item["field"]] = item["value"]

    info = candidate.get("publicBusinessInformation") or {}
    raw_contact = candidate.get("contact") or {}
    raw_social = candidate.get("social") or {}
    contact = {
        "phone": None if "phone" in conflicting_fields else (verified_map.get("phone") or (None if (is_real and research) else raw_contact.get("phone"))),
        "whatsapp": None if "whatsapp" in conflicting_fields else (verified_map.get("whatsapp") or (None if (is_real and research) else raw_contact.get("whatsapp"))),
        "email": None if "email" in conflicting_fields else (verified_map.get("email") or (None if (is_real and research) else raw_contact.get("email"))),
    }
    social = {
        "instagramUrl": None if "instagram_url" in conflicting_fields else (verified_map.get("instagramUrl") or verified_map.get("instagram_url") or (None if (is_real and research) else raw_social.get("instagramUrl"))),
        "facebookUrl": None if "facebook_url" in conflicting_fields else (verified_map.get("facebookUrl") or verified_map.get("facebook_url") or (None if (is_real and research) else raw_social.get("facebookUrl"))),
        "otherSocialUrls": [] if (is_real and research) else (raw_social.get("otherSocialUrls") or []),
    }

    eval_status = (research.get("websiteEvaluation") or {}).get("status")
    raw_status = eval_status or candidate.get("websiteStatus", "unknown")
    website_status = WEBSITE_MAP.get(raw_status, "WEBSITE_UNKNOWN")
    if candidate.get("websiteStatus") == "none_known" and not eval_status:
        website_status = "WEBSITE_UNKNOWN"

    verified=[]; unknown=[]; suggested=[]; reasons=[]; score=0
    niche = candidate.get("niche") or verified_map.get("niche")
    loc = candidate.get("location") or {}
    for label,value in (
        ("business_name", candidate.get("businessName") or verified_map.get("businessName")),
        ("category", niche),
        ("city", loc.get("city")),
        ("country", loc.get("country")),
    ):
        (verified if value else unknown).append({"field":label,"value":value})
    if source_urls: score+=20; reasons.append("At least one public source URL identifies the business.")
    if (candidate.get("businessName") or verified_map.get("businessName")) and loc.get("city"):
        score+=15; reasons.append("Business name and requested location are present.")
    active=bool(
        verified_map.get("description") or verified_map.get("services") or verified_map.get("openingHours")
        or info.get("description") or info.get("servicesOrProducts") or info.get("openingHours")
        or any(contact.values()) or social.get("instagramUrl") or social.get("facebookUrl") or social.get("otherSocialUrls")
    )
    if active: score+=15; reasons.append("A public activity or business-information signal is present.")
    if any(contact.values()): score+=10; reasons.append("A public business contact channel is present.")
    if website_status in {"NO_WEBSITE","WEBSITE_POOR","WEBSITE_OUTDATED"}: score+=35; reasons.append("A source-backed website issue is recorded.")
    elif website_status == "WEBSITE_BASIC": score+=25; reasons.append("A source-backed basic website opportunity is recorded.")
    elif candidate.get("websiteStatus")=="none_known":
        unknown.append({"field":"official_website","reason":"The discovery listing has no website field; absence is not proof of no website."})
        reasons.append("No official website is listed by this discovery source, but ownership/absence remains unverified.")
    if len(source_urls)<2: unknown.append({"field":"identity_confirmation","reason":"Only one source currently supports this record."})
    qualified=score>=60 and website_status in {"NO_WEBSITE","WEBSITE_POOR","WEBSITE_OUTDATED","WEBSITE_BASIC"} and not unknown
    status="QUALIFIED" if qualified else "NEEDS_REVIEW"
    priority="P1" if website_status=="NO_WEBSITE" and score>=70 else "P2" if website_status in {"WEBSITE_POOR","WEBSITE_OUTDATED"} and score>=60 else "P4"

    recommended_template = None
    if niche:
        try:
            from builder.website_builder import _category
            normalized_cat = _category(str(niche)) or str(niche)
            tpl = select_template(normalized_cat)
            recommended_template = tpl.template_id
            suggested.append({"item": tpl.template_id, "reason": f"Category '{niche}' maps to reusable template {tpl.template_id}."})
        except ValueError:
            recommended_template = None
    if not suggested:
        suggested.append({"item":"restaurant-menu-v1","reason":"Restaurant category maps to the existing menu template."})
    suggested.append({"item":"Home, Offerings, Contact","reason":"Suggested only; final pages require verified business information."})

    brief={
        "verified":verified,
        "unknown":unknown,
        "conflicting":conflicting_items,
        "suggested":suggested,
        "contact_ctas":_ctas(contact,social),
        "evidence_sources":source_urls,
        "recommended_template":recommended_template,
    }
    return {
        "qualification_id":None,
        "lead_id":lead_id,
        "status":status,
        "website_status":website_status,
        "website_ownership":"WEBSITE_OWNERSHIP_UNCERTAIN" if website_status=="WEBSITE_UNKNOWN" else "NOT_APPLICABLE",
        "score":score,
        "priority":priority,
        "reasons":reasons,
        "confidence":"high" if len(source_urls)>=2 and not unknown else "medium" if active else "low",
        "evidence":source_urls,
        "content_brief":brief,
        "worker_id":worker_id,
        "job_id":job_id,
        "created_at":_now(),
        "data_classification":classification,
    }

def _ctas(contact,social):
    result=[]
    if contact.get("phone"): result.append("CALL")
    if contact.get("whatsapp"): result.append("WHATSAPP")
    if contact.get("email"): result.append("EMAIL")
    if social.get("instagramUrl"): result.append("INSTAGRAM")
    if not result: result.append("CONTACT_INFORMATION_NEEDED")
    return result

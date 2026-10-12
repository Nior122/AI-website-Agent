"""Turn source-backed Scout candidates into safe, reviewable opportunity briefs."""
from __future__ import annotations
from datetime import datetime, timezone

def _now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

WEBSITE_MAP={"none_known":"WEBSITE_UNKNOWN","unknown":"WEBSITE_UNKNOWN","poor":"WEBSITE_POOR","outdated":"WEBSITE_OUTDATED","basic":"WEBSITE_BASIC","professional":"WEBSITE_MODERN"}

def assess_opportunity(candidate, *, lead_id=None, worker_id=None, job_id=None):
    """Return a qualification record and Builder handoff without asserting missing facts.

    A directory/listing lacking a website field is insufficient evidence for
    `NO_WEBSITE`; it becomes `WEBSITE_UNKNOWN` and requires review.
    """
    source_urls=list(candidate.get("sourceUrls") or [])
    info=candidate.get("publicBusinessInformation") or {}; contact=candidate.get("contact") or {}; social=candidate.get("social") or {}
    raw_status=candidate.get("websiteStatus","unknown"); website_status=WEBSITE_MAP.get(raw_status,"WEBSITE_UNKNOWN")
    verified=[]; unknown=[]; suggested=[]; reasons=[]; score=0
    for label,value in (("business_name",candidate.get("businessName")),("category",candidate.get("niche")),("city",(candidate.get("location") or {}).get("city")),("country",(candidate.get("location") or {}).get("country"))):
        (verified if value else unknown).append({"field":label,"value":value})
    if source_urls: score+=20; reasons.append("At least one public source URL identifies the business.")
    if candidate.get("businessName") and (candidate.get("location") or {}).get("city"): score+=15; reasons.append("Business name and requested location are present.")
    active=bool(info.get("description") or info.get("servicesOrProducts") or info.get("openingHours") or any(contact.values()) or social.get("instagramUrl") or social.get("facebookUrl") or social.get("otherSocialUrls"))
    if active: score+=15; reasons.append("A public activity or business-information signal is present.")
    if any(contact.values()): score+=10; reasons.append("A public business contact channel is present.")
    if website_status in {"WEBSITE_POOR","WEBSITE_OUTDATED"}: score+=35; reasons.append("A source-backed website issue is recorded.")
    elif raw_status=="none_known": unknown.append({"field":"official_website","reason":"The discovery listing has no website field; absence is not proof of no website."}); reasons.append("No official website is listed by this discovery source, but ownership/absence remains unverified.")
    if len(source_urls)<2: unknown.append({"field":"identity_confirmation","reason":"Only one source currently supports this record."})
    qualified=score>=60 and website_status in {"NO_WEBSITE","WEBSITE_POOR","WEBSITE_OUTDATED","WEBSITE_BASIC"} and not unknown
    status="QUALIFIED" if qualified else "NEEDS_REVIEW"
    priority="P1" if website_status=="NO_WEBSITE" and score>=70 else "P2" if website_status in {"WEBSITE_POOR","WEBSITE_OUTDATED"} and score>=60 else "P4"
    suggested.extend([{"item":"restaurant-menu-v1","reason":"Restaurant category maps to the existing menu template."},{"item":"Home, Menu, Contact","reason":"Suggested only; final pages require verified business information."}])
    brief={"verified":verified,"unknown":unknown,"suggested":suggested,"contact_ctas":_ctas(contact,social),"evidence_sources":source_urls,"recommended_template":"restaurant-menu-v1" if str(candidate.get("niche","")).casefold().startswith("restaurant") else None}
    return {"qualification_id":None,"lead_id":lead_id,"status":status,"website_status":website_status,"website_ownership":"WEBSITE_OWNERSHIP_UNCERTAIN" if website_status=="WEBSITE_UNKNOWN" else "NOT_APPLICABLE","score":score,"priority":priority,"reasons":reasons,"confidence":"high" if len(source_urls)>=2 and not unknown else "medium" if active else "low","evidence":source_urls,"content_brief":brief,"worker_id":worker_id,"job_id":job_id,"created_at":_now(),"data_classification":candidate.get("dataClassification","TEST_FIXTURE")}

def _ctas(contact,social):
    result=[]
    if contact.get("phone"): result.append("CALL")
    if contact.get("whatsapp"): result.append("WHATSAPP")
    if social.get("instagramUrl"): result.append("INSTAGRAM")
    if not result: result.append("CONTACT_INFORMATION_NEEDED")
    return result

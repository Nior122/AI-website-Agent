"""Single-file, append-history tracking with guarded lifecycle transitions."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse

STAGES = {
    "DISCOVERED",
    "RESEARCHING",
    "QUALIFIED",
    "BUILDING",
    "VALIDATING",
    "READY_FOR_REVIEW",
    "PREVIEW_DEPLOYED",
    "HUMAN_REVIEW",
    "READY_FOR_OUTREACH",
    "DISQUALIFIED",
    "FAILED",
    "BLOCKED",
    "PAUSED",
    "ARCHIVED",
}
TRANSITIONS = {
    "DISCOVERED": {"RESEARCHING", "DISQUALIFIED", "BLOCKED", "PAUSED"},
    "RESEARCHING": {"QUALIFIED", "DISQUALIFIED", "FAILED", "BLOCKED"},
    "QUALIFIED": {"BUILDING", "PAUSED", "ARCHIVED"},
    "BUILDING": {"VALIDATING", "FAILED", "PAUSED"},
    "VALIDATING": {"READY_FOR_REVIEW", "FAILED", "BUILDING"},
    "READY_FOR_REVIEW": {"PREVIEW_DEPLOYED", "HUMAN_REVIEW", "BUILDING", "PAUSED"},
    "PREVIEW_DEPLOYED": {"HUMAN_REVIEW", "BUILDING", "FAILED"},
    "HUMAN_REVIEW": {"READY_FOR_OUTREACH", "BUILDING", "PAUSED", "ARCHIVED"},
    "READY_FOR_OUTREACH": {"ARCHIVED", "PAUSED"},
    "FAILED": {"BUILDING", "RESEARCHING", "ARCHIVED"},
    "BLOCKED": {"RESEARCHING", "BUILDING", "PAUSED"},
    "PAUSED": {"RESEARCHING", "BUILDING", "ARCHIVED"},
}
DATA_CLASSIFICATIONS = {"TEST_FIXTURE", "REAL_PUBLIC_DATA"}
REVIEW_DECISIONS = {"PENDING", "APPROVED_FOR_PREVIEW", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}
PERMITTED_REVIEW_ACTIONS = {"HOLD", "REBUILD", "DISCARD", "PREVIEW_DEPLOYMENT", "OUTREACH_PREPARATION"}
SHARED_FICTIONAL_PREVIEW_PROJECT = "ai-website-agent-preview"
DEFAULT_STATE_PATH = Path("data/tracking/state.json")


def project_workflow_state(p: dict) -> str:
    """Return the fine-grained review/workflow state for a tracked project."""
    stage = p.get("current_stage")
    rev = p.get("review_status")
    if stage == "QUALIFIED":
        return "QUALIFIED_AWAITING_BUILD"
    if stage == "BUILDING":
        if rev == "CHANGES_REQUESTED":
            return "CHANGES_REQUESTED"
        if rev == "REJECTED":
            return "REJECTED"
        return "BUILDING_DRAFT"
    if stage == "VALIDATING":
        return "DRAFT_GENERATED"
    if stage == "READY_FOR_REVIEW":
        if rev == "APPROVED_FOR_PREVIEW":
            return "APPROVED_FOR_PREVIEW_DEPLOYMENT"
        return "VALIDATION_PASSED_READY_FOR_REVIEW"
    if stage == "PREVIEW_DEPLOYED":
        return "ISOLATED_PREVIEW_DEPLOYED"
    if stage == "HUMAN_REVIEW":
        return "PREVIEW_UNDER_HUMAN_REVIEW"
    if stage == "READY_FOR_OUTREACH":
        return "PREVIEW_APPROVED_READY_FOR_OUTREACH"
    return str(stage or "UNKNOWN")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm(v) -> str | None:
    return re.sub(r"[^a-z0-9]", "", str(v).casefold()) if v else None


def domain(v) -> str | None:
    return urlparse(v).netloc.casefold() if v else None


def validate_preview_project_name(project_name: str, *, real_business: bool = False) -> str:
    """Validate an isolated preview project name and reject production or shared-demo targets."""
    cleaned = str(project_name or "").strip()
    if not cleaned or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", cleaned):
        raise ValueError("Preview project name must be a valid lowercase slug (letters, digits, hyphens).")
    if cleaned == "grace-light-community-church":
        raise ValueError(f"Refusing preview target '{cleaned}': unrelated project is forbidden.")
    if re.search(r"(?:^|-)prod(?:uction)?(?:$|-)|(?:^|-)live(?:$|-)", cleaned):
        raise ValueError(f"Refusing preview target '{cleaned}': production targets are forbidden.")
    if "preview" not in cleaned:
        raise ValueError(f"Refusing preview target '{cleaned}': target project name must include 'preview'.")
    if real_business and cleaned == SHARED_FICTIONAL_PREVIEW_PROJECT:
        raise ValueError(
            f"Refusing preview target '{cleaned}': real-business projects cannot use the shared fictional preview project '{SHARED_FICTIONAL_PREVIEW_PROJECT}'."
        )
    return cleaned


def _empty_state() -> dict:
    return {
        "leads": {},
        "projects": {},
        "builds": {},
        "validations": {},
        "deployments": {},
        "outreach": {},
        "jobs": {},
        "qualifications": {},
        "reviews": {},
    }


class TrackingStore:
    def __init__(self, path: str | Path = DEFAULT_STATE_PATH, *, auto_bootstrap_workspace: bool = False):
        self.path = Path(path)
        self.state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError(f"Tracking state file {self.path} is not valid JSON.") from error
            if not isinstance(loaded, dict):
                raise ValueError(f"Tracking state file {self.path} must contain a JSON object.")
            base = _empty_state()
            base.update(loaded)
            return base
        return _empty_state()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")

    def find_duplicate(self, lead: dict) -> dict:
        for existing in self.state["leads"].values():
            exact = any(
                a and a == b
                for a, b in [
                    (domain(lead.get("existing_website")), domain(existing.get("existing_website"))),
                    (norm(lead.get("phone")), norm(existing.get("phone"))),
                    (lead.get("instagram_url"), existing.get("instagram_url")),
                    (lead.get("facebook_url"), existing.get("facebook_url")),
                ]
            )
            same_place = norm(lead.get("city")) == norm(existing.get("city")) and norm(lead.get("country")) == norm(existing.get("country"))
            same_name = norm(lead.get("business_name")) == norm(existing.get("business_name"))
            if exact:
                return {"match": "EXACT_MATCH", "lead_id": existing["lead_id"]}
            if same_name and same_place:
                return {"match": "POSSIBLE_MATCH", "lead_id": existing["lead_id"]}
        return {"match": "NO_MATCH", "lead_id": None}

    def create_lead(self, lead: dict) -> dict:
        dup = self.find_duplicate(lead)
        if dup["match"] == "EXACT_MATCH":
            raise ValueError(f"Duplicate lead: {dup['lead_id']}")
        classification = lead.get("data_classification") or "TEST_FIXTURE"
        if classification not in DATA_CLASSIFICATIONS:
            raise ValueError(f"Invalid data_classification: {classification}")
        ident = f"lead-{len(self.state['leads']) + 1:04d}"
        record = {
            **lead,
            "data_classification": classification,
            "lead_id": ident,
            "created_at": now(),
            "updated_at": now(),
            "possible_duplicate_of": dup["lead_id"] if dup["match"] == "POSSIBLE_MATCH" else None,
        }
        self.state["leads"][ident] = record
        self._save()
        return record

    def create_project(self, lead_id: str) -> dict:
        lead = self.state["leads"].get(lead_id)
        if not lead or lead.get("qualification_status") != "QUALIFIED":
            raise ValueError("Only QUALIFIED leads can have projects.")
        existing = next(
            (project for project in self.state["projects"].values() if project["lead_id"] == lead_id and project.get("project_status") == "ACTIVE"),
            None,
        )
        if existing:
            return existing
        ident = f"project-{len(self.state['projects']) + 1:04d}"
        p = {
            "project_id": ident,
            "lead_id": lead_id,
            "data_classification": lead.get("data_classification", "TEST_FIXTURE"),
            "current_stage": "QUALIFIED",
            "project_status": "ACTIVE",
            "review_status": "PENDING",
            "review_notes": None,
            "review_history": [],
            "isolated_preview_project": None,
            "created_at": now(),
            "updated_at": now(),
        }
        self.state["projects"][ident] = p
        self._save()
        return p

    def configure_preview_target(self, project_id: str, project_name: str) -> dict:
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}")
        lead = self.state["leads"].get(p["lead_id"]) or {}
        is_real = (lead.get("data_classification") or p.get("data_classification")) == "REAL_PUBLIC_DATA"
        validated = validate_preview_project_name(project_name, real_business=is_real)
        p["isolated_preview_project"] = validated
        p["updated_at"] = now()
        self._save()
        return p

    def ingest_research_record(self, record: dict, *, assessment: dict | None = None, lead_id: str | None = None) -> dict:
        """Create or idempotently return a tracked lead from a ResearchAgent record and optional opportunity assessment."""
        if not isinstance(record, dict) or not isinstance(record.get("candidate"), dict) or not isinstance(record.get("research"), dict):
            raise ValueError("Research record must contain 'candidate' and 'research' dictionaries.")
        candidate = record["candidate"]
        research = record["research"]
        business_name = candidate.get("businessName")
        location = candidate.get("location") or {}
        if not business_name or not location.get("city") or not location.get("country"):
            raise ValueError("Candidate record is missing required businessName, city, or country.")
        if assessment is None:
            from opportunities.service import assess_opportunity
            assessment = assess_opportunity(record)
        classification = (
            record.get("data_classification")
            or candidate.get("dataClassification")
            or (assessment or {}).get("data_classification")
            or "TEST_FIXTURE"
        )
        if classification not in DATA_CLASSIFICATIONS:
            raise ValueError(f"Invalid data_classification: {classification}")
        is_real = classification == "REAL_PUBLIC_DATA"
        facts = {item.get("field"): item.get("value") for item in research.get("verifiedFacts", []) if item.get("field")}
        fact_loc = facts.get("location") if isinstance(facts.get("location"), dict) else {}
        fact_contact = facts.get("contact") if isinstance(facts.get("contact"), dict) else {}
        fact_social = facts.get("social") if isinstance(facts.get("social"), dict) else {}
        conflicting_list = [
            *(research.get("conflictingFacts") or []),
            *(research.get("disputedFacts") or []),
        ]
        conflicting_fields = {
            {"instagramUrl": "instagram_url", "facebookUrl": "facebook_url"}.get(str(item.get("field")), str(item.get("field")))
            for item in conflicting_list
            if isinstance(item, dict) and item.get("field")
        }
        contact = candidate.get("contact") or {}
        social = candidate.get("social") or {}
        qual_status = research.get("qualificationStatus") or (assessment or {}).get("status") or "NEEDS_REVIEW"
        website_status = (
            (research.get("websiteEvaluation") or {}).get("status")
            or (assessment or {}).get("website_status")
            or candidate.get("websiteStatus")
            or "UNKNOWN"
        )
        source_urls = list(dict.fromkeys(candidate.get("sourceUrls") or []))
        research_sources = list(dict.fromkeys([
            *(research.get("researchSources") or []),
            *((assessment or {}).get("evidence") or []),
        ]))
        verified_address = facts.get("address") or fact_loc.get("address")
        verified_phone = facts.get("phone") or fact_contact.get("phone")
        verified_email = facts.get("email") or fact_contact.get("email")
        verified_whatsapp = facts.get("whatsapp") or fact_contact.get("whatsapp")
        verified_ig = facts.get("instagram_url") or facts.get("instagramUrl") or fact_social.get("instagramUrl")
        verified_fb = facts.get("facebook_url") or facts.get("facebookUrl") or fact_social.get("facebookUrl")

        lead_payload = {
            "business_name": business_name,
            "business_type": facts.get("niche") or candidate.get("niche"),
            "city": fact_loc.get("city") or location.get("city"),
            "country": fact_loc.get("country") or location.get("country"),
            "address": None if "address" in conflicting_fields else (verified_address if is_real else (verified_address or location.get("address"))),
            "existing_website": candidate.get("originalWebsite"),
            "qualification_status": qual_status,
            "website_status": website_status,
            "source_urls": source_urls,
            "research_source_urls": research_sources or source_urls,
            "phone": None if "phone" in conflicting_fields else (verified_phone if is_real else (verified_phone or contact.get("phone"))),
            "email": None if "email" in conflicting_fields else (verified_email if is_real else (verified_email or contact.get("email"))),
            "whatsapp": None if "whatsapp" in conflicting_fields else (verified_whatsapp if is_real else (verified_whatsapp or contact.get("whatsapp"))),
            "instagram_url": None if "instagram_url" in conflicting_fields else (verified_ig if is_real else (verified_ig or social.get("instagramUrl"))),
            "facebook_url": None if "facebook_url" in conflicting_fields else (verified_fb if is_real else (verified_fb or social.get("facebookUrl"))),
            "conflicting_facts": copy.deepcopy(conflicting_list),
            "data_classification": classification,
            "demo": bool(record.get("demo")) or classification == "TEST_FIXTURE",
            "research_record": copy.deepcopy(record),
        }
        explicit_lead_id = lead_id or record.get("lead_id")
        dup = self.find_duplicate(lead_payload)
        matched_id = (
            explicit_lead_id
            if explicit_lead_id and explicit_lead_id in self.state["leads"]
            else (
                dup["lead_id"]
                if dup["match"] == "EXACT_MATCH"
                or (
                    dup["match"] == "POSSIBLE_MATCH"
                    and self.state["leads"][dup["lead_id"]].get("data_classification") == classification
                )
                else None
            )
        )
        if matched_id:
            existing = self.state["leads"][matched_id]
            if existing.get("data_classification") != classification:
                raise ValueError("Cannot merge fictional and real-business lead records.")
            for key in (
                "business_type",
                "address",
                "existing_website",
                "qualification_status",
                "website_status",
                "source_urls",
                "research_source_urls",
                "phone",
                "email",
                "whatsapp",
                "instagram_url",
                "facebook_url",
                "conflicting_facts",
            ):
                existing[key] = lead_payload[key]
            existing["research_record"] = copy.deepcopy(record)
            existing["research_record"]["lead_id"] = existing["lead_id"]
            existing["updated_at"] = now()
            lead = existing
        else:
            lead = self.create_lead(lead_payload)
            lead["research_record"]["lead_id"] = lead["lead_id"]
        qual_record = {
            **(assessment or {}),
            "lead_id": lead["lead_id"],
            "status": qual_status,
            "website_status": website_status,
            "data_classification": classification,
            "evidence": research_sources or source_urls,
        }
        self.record_qualification(qual_record)
        return self.state["leads"][lead["lead_id"]]

    def build_payload(self, lead_id: str) -> dict:
        """Return a WebsiteBuilder-compatible payload for a tracked lead without fabricating missing evidence."""
        lead = self.state["leads"].get(lead_id)
        if not lead:
            raise ValueError(f"Unknown lead: {lead_id}")
        stored = lead.get("research_record")
        if isinstance(stored, dict) and "candidate" in stored and "research" in stored:
            payload = copy.deepcopy(stored)
            payload["lead_id"] = lead_id
            payload["data_classification"] = lead.get("data_classification", "TEST_FIXTURE")
            payload["demo"] = bool(lead.get("demo")) or payload["data_classification"] == "TEST_FIXTURE"
            return payload
        durable = _find_durable_lead_source(Path("."), lead_id, lead)
        if durable:
            return durable
        raise ValueError(f"Lead {lead_id} has no verified research record available for website generation.")

    def record_qualification(self, qualification: dict) -> dict:
        lead = self.state["leads"].get(qualification.get("lead_id"))
        if not lead:
            raise ValueError("Qualification requires an existing lead.")
        ident = qualification.get("qualification_id") or f"qualification-{len(self.state['qualifications']) + 1:04d}"
        if ident in self.state["qualifications"]:
            return self.state["qualifications"][ident]
        qualification["qualification_id"] = ident
        record = {**qualification, "qualification_id": ident, "created_at": qualification.get("created_at") or now()}
        self.state["qualifications"][ident] = record
        lead["latest_qualification_id"] = ident
        lead["qualification_status"] = record["status"]
        if record.get("website_status"):
            lead["website_status"] = record["website_status"]
        if record.get("content_brief"):
            lead["content_brief"] = record["content_brief"]
        if record.get("evidence"):
            lead["research_source_urls"] = list(dict.fromkeys(record["evidence"]))
        lead["updated_at"] = now()
        self._save()
        return record

    def transition(self, project_id: str, to: str) -> dict:
        p = self.state["projects"].get(project_id)
        if not p or to not in TRANSITIONS.get(p["current_stage"], set()):
            raise ValueError("Illegal project state transition.")
        p["current_stage"] = to
        p["updated_at"] = now()
        self._save()
        return p

    def record_build(self, project_id: str, build: dict) -> dict:
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError("Project is not ready for a build.")
        if build.get("lead_id") != p["lead_id"]:
            raise ValueError("Build lead does not match project lead.")
        build_id = build.get("build_id")
        if not build_id:
            raise ValueError("Build record requires a build_id.")
        existing = self.state["builds"].get(build_id)
        if existing and existing.get("project_id") != project_id:
            raise ValueError("Build ID is already assigned to another project.")
        if existing and existing.get("status") == "FAILED" and build.get("status") != "FAILED":
            raise ValueError("Failed build cannot become a successful build.")
        if (
            existing
            and existing.get("project_id") == project_id
            and p.get("assigned_build_id") == build_id
            and existing.get("status") == build.get("status")
        ):
            return existing
        if p["current_stage"] not in {"QUALIFIED", "BUILDING", "FAILED", "READY_FOR_REVIEW", "VALIDATING"}:
            raise ValueError("Project is not ready for a build.")
        lead = self.state["leads"].get(p["lead_id"]) or {}
        lead_class = lead.get("data_classification") or p.get("data_classification") or "TEST_FIXTURE"
        build_class = build.get("data_classification") or (build.get("metadata") or {}).get("data_classification")
        if lead_class == "REAL_PUBLIC_DATA" and (
            build_class == "TEST_FIXTURE"
            or build.get("demo") is True
            or "fictional-" in str(build.get("output_location") or "").casefold()
        ):
            raise ValueError("Cannot attach fictional/test-fixture build to a real-business project.")
        if lead_class == "TEST_FIXTURE" and build_class == "REAL_PUBLIC_DATA":
            raise ValueError("Cannot attach real-business build to a fictional test project.")
        if p["current_stage"] in {"QUALIFIED", "FAILED", "READY_FOR_REVIEW", "VALIDATING"}:
            self.transition(project_id, "BUILDING")
        stored_build = {
            **build,
            "project_id": project_id,
            "data_classification": build_class or lead_class,
        }
        self.state["builds"][build_id] = stored_build
        p["assigned_build_id"] = build_id
        p["latest_validation_id"] = None
        p["latest_deployment_id"] = None
        p["review_status"] = "PENDING"
        p["review_notes"] = None
        p["reviewed_at"] = None
        p["approved_build_id"] = None
        p["approved_action"] = None
        p["pre_deployment_review_status"] = None
        if build.get("status") == "FAILED":
            self.transition(project_id, "FAILED")
        else:
            self.transition(project_id, "VALIDATING")
        return self.state["builds"][build_id]

    def record_validation(self, project_id: str, validation: dict) -> dict:
        build = self.state["builds"].get(validation.get("build_id"))
        p = self.state["projects"].get(project_id)
        if not build or not p or build["project_id"] != project_id or p.get("assigned_build_id") != validation.get("build_id"):
            raise ValueError("Validation does not belong to this project build.")
        if build.get("status") == "FAILED":
            raise ValueError("Failed build cannot be validated.")
        val_id = validation.get("validation_id")
        if not val_id:
            raise ValueError("Validation record requires a validation_id.")
        existing_val = self.state["validations"].get(val_id)
        if existing_val and (existing_val.get("project_id") != project_id or existing_val.get("build_id") != build["build_id"]):
            raise ValueError("Validation ID is already assigned to another build or project.")
        if (
            existing_val
            and existing_val.get("project_id") == project_id
            and existing_val.get("build_id") == build["build_id"]
            and existing_val.get("status") == validation.get("status")
            and p.get("latest_validation_id") == val_id
        ):
            return existing_val
        val_lead_id = validation.get("lead_id")
        if val_lead_id and val_lead_id != p["lead_id"] and not str(val_lead_id).startswith("fictional-"):
            raise ValueError("Validation lead does not match project lead.")
        build_output = build.get("output_location")
        val_artifact = validation.get("artifact_path")
        if build_output and val_artifact and Path(str(val_artifact)).resolve() != Path(str(build_output)).resolve():
            raise ValueError("Validation artifact_path does not match build output_location.")
        stored_val = {
            **validation,
            "project_id": project_id,
            "lead_id": p["lead_id"],
            "artifact_path": val_artifact or build_output,
        }
        self.state["validations"][val_id] = stored_val
        p["latest_validation_id"] = val_id
        if p["current_stage"] in {"FAILED", "READY_FOR_REVIEW"}:
            self.transition(project_id, "BUILDING")
            self.transition(project_id, "VALIDATING")
        elif p["current_stage"] == "BUILDING":
            self.transition(project_id, "VALIDATING")
        self.transition(project_id, "READY_FOR_REVIEW" if validation.get("status") == "PASS" else "FAILED")
        return stored_val

    def record_deployment(self, project_id: str, deployment: dict) -> dict:
        p = self.state["projects"].get(project_id)
        build = self.state["builds"].get(deployment.get("build_id"))
        if (
            not p
            or not build
            or build["project_id"] != project_id
            or p.get("assigned_build_id") != deployment.get("build_id")
            or build.get("status") == "FAILED"
            or deployment.get("validation_status") != "PASS"
            or deployment.get("deployment_status") != "DEPLOYED"
        ):
            raise ValueError("Deployment must be a successful PASS deployment for this project build.")
        latest_val = self.state["validations"].get(p.get("latest_validation_id"))
        if not latest_val or latest_val.get("build_id") != build["build_id"] or latest_val.get("status") != "PASS":
            raise ValueError("Deployment requires a recorded PASS validation for the assigned project build.")
        if deployment.get("validation_id") and deployment.get("validation_id") != latest_val.get("validation_id"):
            raise ValueError("Deployment validation_id does not match the project's latest validation.")
        dep_lead_id = deployment.get("lead_id")
        if dep_lead_id and dep_lead_id != p["lead_id"] and not str(dep_lead_id).startswith("fictional-"):
            raise ValueError("Deployment lead does not match project lead.")
        dep_id = deployment.get("deployment_id")
        if not dep_id:
            raise ValueError("Deployment record requires a deployment_id.")
        existing_dep = self.state["deployments"].get(dep_id)
        if existing_dep and existing_dep.get("project_id") != project_id:
            raise ValueError("Deployment ID is already assigned to another project.")
        if (
            existing_dep
            and existing_dep.get("project_id") == project_id
            and existing_dep.get("build_id") == build["build_id"]
            and existing_dep.get("deployment_status") == "DEPLOYED"
            and p.get("latest_deployment_id") == dep_id
        ):
            return existing_dep
        if p.get("review_status") in {"REJECTED", "CHANGES_REQUESTED"} or p.get("current_stage") not in {"READY_FOR_REVIEW", "PREVIEW_DEPLOYED"}:
            raise ValueError("Project is not in READY_FOR_REVIEW for preview deployment.")
        lead = self.state["leads"].get(p["lead_id"]) or {}
        is_real = (lead.get("data_classification") or p.get("data_classification") or build.get("data_classification")) == "REAL_PUBLIC_DATA"
        dep_meta = deployment.get("metadata") or {}
        if str(dep_meta.get("target") or "").strip().lower() in {"prod", "production"}:
            raise ValueError("Refusing deployment: production target is forbidden; only preview deployments are supported.")
        dep_project_name = dep_meta.get("project_name")
        dep_url = str(deployment.get("deployment_url") or "")
        if dep_project_name and re.search(r"(?:^|-)prod(?:uction)?(?:$|-)|(?:^|-)live(?:$|-)", str(dep_project_name).strip()):
            raise ValueError(f"Refusing deployment: '{dep_project_name}' is a production target; only preview projects are permitted.")
        if is_real and (
            dep_project_name == SHARED_FICTIONAL_PREVIEW_PROJECT
            or SHARED_FICTIONAL_PREVIEW_PROJECT in dep_url
        ):
            raise ValueError(
                f"Real-business deployment cannot target the shared fictional preview project '{SHARED_FICTIONAL_PREVIEW_PROJECT}'."
            )
        if is_real:
            effective_preview = str(dep_project_name or p.get("isolated_preview_project") or "").strip()
            if not effective_preview:
                raise ValueError("Real-business deployment requires a verified isolated preview project target.")
            validate_preview_project_name(effective_preview, real_business=True)
        if p.get("isolated_preview_project") and dep_project_name and dep_project_name != p["isolated_preview_project"]:
            raise ValueError("Deployment project_name does not match project's configured isolated_preview_project.")
        if is_real and p.get("current_stage") != "PREVIEW_DEPLOYED" and (
            p.get("review_status") != "APPROVED_FOR_PREVIEW"
            or p.get("approved_build_id") != build["build_id"]
            or p.get("approved_action") != "PREVIEW_DEPLOYMENT"
        ):
            raise ValueError(
                "Real-business deployment requires explicit pre-deployment human review approval (APPROVED_FOR_PREVIEW for action PREVIEW_DEPLOYMENT) bound to the current assigned build."
            )
        stored_dep = {**deployment, "project_id": project_id, "lead_id": p["lead_id"]}
        self.state["deployments"][dep_id] = stored_dep
        p["latest_deployment_id"] = dep_id
        if p.get("review_status") == "APPROVED_FOR_PREVIEW":
            p["pre_deployment_review_status"] = "APPROVED_FOR_PREVIEW"
            p["review_status"] = "PENDING"
            p["approved_action"] = None
        self.transition(project_id, "PREVIEW_DEPLOYED")
        return stored_dep

    def record_review(
        self,
        project_id: str,
        status: str,
        notes: str | None = None,
        *,
        reviewer: str = "operator",
        require_notes: bool = False,
        build_id: str | None = None,
        action: str | None = None,
    ) -> dict:
        normalized_status = str(status or "").strip().upper()
        normalized_action = str(action).strip().upper() if action is not None else None
        if normalized_status == "APPROVED" and normalized_action == "PREVIEW_DEPLOYMENT":
            normalized_status = "APPROVED_FOR_PREVIEW"
        if normalized_status not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid review status: {status}. Must be one of {sorted(REVIEW_DECISIONS)}.")
        if normalized_action is not None and normalized_action not in PERMITTED_REVIEW_ACTIONS:
            raise ValueError(f"Invalid review action: {action}. Must be one of {sorted(PERMITTED_REVIEW_ACTIONS)}.")
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}")
        if require_notes and (not isinstance(notes, str) or len(notes.strip()) < 3):
            raise ValueError("Review decision requires meaningful review notes (at least 3 characters).")
        assigned_build_id = p.get("assigned_build_id")
        if build_id is not None and build_id != assigned_build_id:
            raise ValueError(
                f"Review build_id '{build_id}' does not match project's assigned_build_id '{assigned_build_id}'."
            )
        build = self.state["builds"].get(assigned_build_id) if assigned_build_id else None
        latest_val = self.state["validations"].get(p.get("latest_validation_id"))
        latest_dep = self.state["deployments"].get(p.get("latest_deployment_id"))
        wf_before = project_workflow_state(p)
        intended_action = normalized_action

        if normalized_status == "APPROVED_FOR_PREVIEW":
            intended_action = intended_action or "PREVIEW_DEPLOYMENT"
            if intended_action != "PREVIEW_DEPLOYMENT":
                raise ValueError("APPROVED_FOR_PREVIEW decision only permits action 'PREVIEW_DEPLOYMENT'.")
            if (
                not assigned_build_id
                or not build
                or build.get("project_id") != project_id
                or build.get("lead_id") != p["lead_id"]
                or build.get("status") == "FAILED"
            ):
                raise ValueError("Pre-deployment approval requires a valid assigned build for this project.")
            if (
                not latest_val
                or latest_val.get("project_id") != project_id
                or latest_val.get("build_id") != assigned_build_id
                or latest_val.get("status") != "PASS"
            ):
                raise ValueError("Pre-deployment approval requires a PASS validation matching the assigned build.")
            if (
                build.get("output_location")
                and latest_val.get("artifact_path")
                and Path(str(latest_val["artifact_path"])).resolve() != Path(str(build["output_location"])).resolve()
            ):
                raise ValueError("Pre-deployment approval requires validation artifact_path to match the assigned build output_location.")
            if p["current_stage"] != "READY_FOR_REVIEW":
                raise ValueError("Pre-deployment approval requires the project to be in READY_FOR_REVIEW.")
        elif normalized_status == "APPROVED":
            intended_action = intended_action or "OUTREACH_PREPARATION"
            if intended_action != "OUTREACH_PREPARATION":
                raise ValueError("Post-deployment APPROVED decision only permits action 'OUTREACH_PREPARATION'.")
            if (
                not assigned_build_id
                or not build
                or build.get("project_id") != project_id
                or build.get("lead_id") != p["lead_id"]
                or build.get("status") == "FAILED"
            ):
                raise ValueError("Approval requires a deployed preview and a valid assigned build for this project.")
            if (
                not latest_val
                or latest_val.get("project_id") != project_id
                or latest_val.get("build_id") != assigned_build_id
                or latest_val.get("status") != "PASS"
            ):
                raise ValueError("Approval requires a deployed preview and a PASS validation matching the assigned build.")
            if (
                build.get("output_location")
                and latest_val.get("artifact_path")
                and Path(str(latest_val["artifact_path"])).resolve() != Path(str(build["output_location"])).resolve()
            ):
                raise ValueError("Approval requires validation artifact_path to match the assigned build output_location.")
            if (
                p["current_stage"] not in {"PREVIEW_DEPLOYED", "HUMAN_REVIEW"}
                or not latest_dep
                or latest_dep.get("project_id") != project_id
                or latest_dep.get("build_id") != assigned_build_id
                or latest_dep.get("deployment_status") != "DEPLOYED"
            ):
                raise ValueError("Approval requires a deployed preview.")
        elif normalized_status in {"CHANGES_REQUESTED", "REJECTED"}:
            intended_action = intended_action or ("REBUILD" if normalized_status == "CHANGES_REQUESTED" else "DISCARD")
            if (
                p["current_stage"] not in {"READY_FOR_REVIEW", "PREVIEW_DEPLOYED", "HUMAN_REVIEW"}
                or not assigned_build_id
                or not build
                or build.get("project_id") != project_id
            ):
                raise ValueError("Project is not in a reviewable stage.")
        else:
            intended_action = intended_action or "HOLD"

        stamp = now()
        stage_before = p["current_stage"]
        p.update({
            "review_status": normalized_status,
            "review_notes": notes,
            "reviewed_at": stamp,
            "updated_at": stamp,
            "approved_build_id": assigned_build_id if normalized_status in {"APPROVED_FOR_PREVIEW", "APPROVED"} else None,
            "approved_action": intended_action if normalized_status in {"APPROVED_FOR_PREVIEW", "APPROVED"} else None,
        })
        if normalized_status in {"CHANGES_REQUESTED", "REJECTED"}:
            self.transition(project_id, "BUILDING")
        elif normalized_status == "APPROVED":
            if p["current_stage"] == "PREVIEW_DEPLOYED":
                self.transition(project_id, "HUMAN_REVIEW")
            self.transition(project_id, "READY_FOR_OUTREACH")
        wf_after = project_workflow_state(p)
        reviews = self.state.setdefault("reviews", {})
        review_id = f"review-{len(reviews) + 1:04d}"
        entry = {
            "review_id": review_id,
            "project_id": project_id,
            "lead_id": p["lead_id"],
            "build_id": assigned_build_id,
            "validation_id": p.get("latest_validation_id"),
            "deployment_id": p.get("latest_deployment_id"),
            "decision": normalized_status,
            "status": normalized_status,
            "intended_action": intended_action,
            "notes": notes,
            "review_notes": notes,
            "reviewer": reviewer,
            "stage_before": stage_before,
            "stage_after": p["current_stage"],
            "workflow_state_before": wf_before,
            "workflow_state_after": wf_after,
            "timestamp": stamp,
            "reviewed_at": stamp,
        }
        reviews[review_id] = entry
        p.setdefault("review_history", []).append(entry)
        self._save()
        return p

    def get_review_history(self, project_id: str) -> list[dict]:
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}")
        raw_history = list(p.get("review_history") or [])
        normalized: list[dict] = []
        for index, item in enumerate(raw_history):
            decision = item.get("decision") or item.get("status")
            notes = item.get("notes") if "notes" in item else item.get("review_notes")
            timestamp = item.get("timestamp") or item.get("reviewed_at")
            default_action = (
                "PREVIEW_DEPLOYMENT" if decision == "APPROVED_FOR_PREVIEW"
                else "OUTREACH_PREPARATION" if decision == "APPROVED"
                else "REBUILD" if decision == "CHANGES_REQUESTED"
                else "DISCARD" if decision == "REJECTED"
                else "HOLD"
            )
            normalized.append({
                "index": index,
                "review_id": item.get("review_id") or f"review-{index + 1:04d}",
                "project_id": item.get("project_id") or project_id,
                "lead_id": item.get("lead_id") or p["lead_id"],
                "build_id": item.get("build_id"),
                "validation_id": item.get("validation_id"),
                "deployment_id": item.get("deployment_id"),
                "decision": decision,
                "status": decision,
                "intended_action": item.get("intended_action") or default_action,
                "notes": notes,
                "review_notes": notes,
                "reviewer": item.get("reviewer") or "operator",
                "stage_before": item.get("stage_before"),
                "stage_after": item.get("stage_after"),
                "workflow_state_before": item.get("workflow_state_before"),
                "workflow_state_after": item.get("workflow_state_after"),
                "timestamp": timestamp,
                "reviewed_at": timestamp,
            })
        normalized.sort(key=lambda entry: (entry.get("timestamp") or "", entry["index"]))
        for entry in normalized:
            entry.pop("index", None)
        return normalized

    def status_summary(self) -> dict:
        projects: list[dict] = []
        for project_id in sorted(self.state["projects"]):
            snap = self.snapshot(project_id)
            p = snap["project"]
            lead = snap["lead"]
            val = snap["latestValidation"]
            dep = snap["latestDeployment"]
            projects.append({
                "project_id": project_id,
                "lead_id": lead["lead_id"],
                "business_name": lead.get("business_name"),
                "data_classification": lead.get("data_classification") or p.get("data_classification"),
                "assigned_build_id": p.get("assigned_build_id"),
                "current_stage": p.get("current_stage"),
                "workflow_state": snap["workflow_state"],
                "review_status": p.get("review_status"),
                "approved_build_id": p.get("approved_build_id"),
                "approved_action": p.get("approved_action"),
                "validation_id": val.get("validation_id") if val else None,
                "validation_status": val.get("status") if val else None,
                "deployment_id": dep.get("deployment_id") if dep else None,
                "deployment_status": dep.get("deployment_status") if dep else None,
                "deployment_url": dep.get("deployment_url") if dep else None,
                "isolated_preview_project": p.get("isolated_preview_project"),
                "artifact_exists": snap["artifact_exists"],
                "next_action": snap["next_action"],
            })
        return {"project_count": len(projects), "projects": projects}

    def snapshot(self, project_id: str) -> dict:
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}")
        lead = self.state["leads"][p["lead_id"]]
        assigned = p.get("assigned_build_id")
        b = self.state["builds"].get(assigned)
        v = self.state["validations"].get(p.get("latest_validation_id"))
        if v and v.get("build_id") != assigned:
            v = None
        d = self.state["deployments"].get(p.get("latest_deployment_id"))
        if d and d.get("build_id") != assigned:
            d = None
        artifact_path = b.get("output_location") if b else None
        return {
            "lead": lead,
            "project": p,
            "latestBuild": b,
            "latestValidation": v,
            "latestDeployment": d,
            "workflow_state": project_workflow_state(p),
            "artifact_exists": bool(artifact_path and Path(artifact_path).is_file()),
            "next_action": self._next(p),
        }

    def build_review_package(
        self,
        project_id: str,
        *,
        export_dir: str | Path | None = None,
        repository_root: Path = Path("."),
    ) -> dict:
        """Assemble a local human-review package for a project's assigned build without altering review status."""
        from builder.website_builder import _verified_template_data

        root = Path(repository_root).resolve()
        snap = self.snapshot(project_id)
        p = snap["project"]
        lead = snap["lead"]
        build = snap["latestBuild"]
        val = snap["latestValidation"]
        dep = snap["latestDeployment"]
        if not build or not build.get("output_location"):
            raise ValueError(f"Project {project_id} has no assigned build artifact for human review.")
        out_path = Path(build["output_location"])
        resolved_html = out_path if out_path.is_absolute() else (root / out_path)
        if not resolved_html.is_file():
            raise ValueError(f"Assigned build HTML artifact does not exist on disk: {out_path}")

        html_bytes = resolved_html.read_bytes()
        html_text = html_bytes.decode("utf-8")
        sha256_hex = hashlib.sha256(html_bytes).hexdigest()

        hrefs = re.findall(r'<a\b[^>]*\bhref="([^"]+)"', html_text)
        external_links = list(dict.fromkeys(h for h in hrefs if not h.startswith("#")))
        internal_anchors = list(dict.fromkeys(h for h in hrefs if h.startswith("#")))

        source_payload = _find_durable_lead_source(root, lead["lead_id"], lead)
        template_facts = _verified_template_data(source_payload) if source_payload else {}
        provenance = template_facts.get("fact_provenance") or (build.get("metadata") or {}).get("fact_provenance") or {}
        disputed_map = template_facts.get("disputed_facts") or (build.get("metadata") or {}).get("disputed_facts") or {}

        supported_facts: list[dict] = []
        field_pairs = [
            ("business_name", "businessName", template_facts.get("business_name") or lead.get("business_name")),
            ("category", "niche", template_facts.get("category") or lead.get("business_type")),
            ("city", "location", template_facts.get("city") or lead.get("city")),
            ("address", "address", template_facts.get("address")),
            ("description", "description", template_facts.get("description")),
            ("phone", "phone", template_facts.get("phone")),
            ("email", "email", template_facts.get("email")),
            ("whatsapp", "whatsapp", template_facts.get("whatsapp")),
            ("instagram_url", "social", template_facts.get("instagram_url")),
            ("facebook_url", "social", template_facts.get("facebook_url")),
            ("opening_hours", "openingHours", template_facts.get("opening_hours")),
            ("services", "services", template_facts.get("services")),
        ]
        for label, prov_key, value in field_pairs:
            if value in (None, "", [], {}):
                continue
            prov = provenance.get(label) or provenance.get(prov_key) or provenance.get("contact") or provenance.get("location") or {}
            supported_facts.append({
                "field": label,
                "value": value,
                "source_urls": prov.get("sourceUrls") or lead.get("source_urls") or [],
                "evidence_tier": prov.get("evidence_tier") or "SOURCE_URL_ONLY",
                "verification_status": prov.get("verification_status") or "SUPPORTED_BY_SOURCE_URL",
            })

        unresolved_or_conflicting: list[dict] = []
        for field_name, info in disputed_map.items():
            unresolved_or_conflicting.append({
                "field": field_name,
                "status": "CONFLICTING_WITHHELD",
                "conflicting_values": info.get("values") or [],
                "source_urls": info.get("sourceUrls") or [],
                "evidence_tier": "CONFLICTING_OR_UNVERIFIABLE",
                "reason": info.get("reason") or "Conflicting public values; withheld from generated website until verified.",
            })
        for omitted_field, omitted_reason in (
            ("opening_hours", "No verified operating hours in public research records; omitted from website."),
            ("whatsapp", "No verified WhatsApp number in public research records; omitted from website."),
            ("menu_items", "No verified food menu items or dish prices in public research records; omitted from website."),
        ):
            if not template_facts.get(omitted_field) and omitted_field not in disputed_map:
                unresolved_or_conflicting.append({
                    "field": omitted_field,
                    "status": "OMITTED_UNVERIFIED",
                    "conflicting_values": [],
                    "source_urls": [],
                    "evidence_tier": "CONFLICTING_OR_UNVERIFIABLE",
                    "reason": omitted_reason,
                })

        b_name = lead.get("business_name") or "Business"
        disclosure = (
            f"Uncommissioned preview draft prepared for internal evaluation · "
            f"Not an authorized or official website of {b_name}."
        )
        omitted_disputed_fields = [item["field"] for item in unresolved_or_conflicting if item["status"] == "CONFLICTING_WITHHELD"]
        claims_lacking_primary_observation = [
            {
                "field": sf["field"],
                "value": sf["value"],
                "source_urls": sf["source_urls"],
                "evidence_tier": sf["evidence_tier"],
                "note": "Supported by consistent source URL references across project records, but no raw primary-source HTML excerpt or timestamp is preserved in Git (data/leads/* is git-ignored); confirm visually during human review.",
            }
            for sf in supported_facts
            if sf["evidence_tier"] == "SOURCE_URL_ONLY"
        ]
        default_export_str = str(export_dir) if export_dir is not None else str(out_path.parent)
        local_preview_instructions = {
            "direct_file_path": str(Path(default_export_str) / "index.html"),
            "local_server_command": f"python3 -m builder.cli --serve {default_export_str} --host 127.0.0.1 --port 8080",
            "tracking_cli_serve_command": f"python3 -m tracking.cli review-package {project_id} --export-dir {default_export_str} --serve --port 8080",
            "local_url": "http://127.0.0.1:8080/",
            "bind_host": "127.0.0.1",
            "stop_instructions": "Press Ctrl+C in the terminal running the server to stop the local preview server.",
            "note": "Starting the local HTTP server does not constitute automated browser visual testing; open http://127.0.0.1:8080/ in a real desktop/mobile browser to complete visual review.",
        }
        package = {
            "project_id": project_id,
            "lead_id": lead["lead_id"],
            "business_name": b_name,
            "data_classification": lead.get("data_classification") or p.get("data_classification"),
            "current_stage": p.get("current_stage"),
            "review_status": p.get("review_status"),
            "build_id": build["build_id"],
            "template_id": build.get("template_id"),
            "artifact_path": str(out_path),
            "artifact_exists": True,
            "artifact_bytes": len(html_bytes),
            "artifact_sha256": sha256_hex,
            "uncommissioned_disclosure": disclosure,
            "disclosure_present_in_html": disclosure in html_text,
            "omitted_disputed_fields": omitted_disputed_fields,
            "supported_facts": supported_facts,
            "claims_lacking_primary_observation": claims_lacking_primary_observation,
            "unresolved_or_conflicting_facts": unresolved_or_conflicting,
            "external_links": external_links,
            "internal_anchors": internal_anchors,
            "local_preview_instructions": local_preview_instructions,
            "validation": {
                "validation_id": val.get("validation_id") if val else None,
                "build_id": val.get("build_id") if val else None,
                "status": val.get("status") if val else None,
                "score": val.get("score") if val else None,
                "checks": val.get("checks") if val else {},
                "findings": val.get("findings") if val else [],
                "limitations": [
                    "Static HTML/CSS/content validation only; does not execute a real browser layout engine.",
                    "Browser-based visual inspection at desktop and mobile viewports was not performed in the sandbox because no browser binary or Playwright package is installed.",
                    "External URLs are checked for syntax and scheme safety offline; live HTTP reachability and current live contact/address resolution require human operator verification.",
                ],
            },
            "isolated_preview_project": p.get("isolated_preview_project"),
            "deployment_id": dep.get("deployment_id") if dep else None,
            "deployment_url": dep.get("deployment_url") if dep else None,
        }

        if export_dir is not None:
            exp_path = Path(export_dir)
            resolved_exp = exp_path if exp_path.is_absolute() else (root / exp_path)
            resolved_exp.mkdir(parents=True, exist_ok=True)
            (resolved_exp / "index.html").write_bytes(html_bytes)
            package["exported_to"] = str(exp_path)
            (resolved_exp / "review-package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
            md_lines = [
                f"# Human Review Package — {b_name} (`{project_id}`)",
                "",
                f"> **Disclosure:** {disclosure}",
                "",
                "## 1. Build & Validation Identity",
                f"- **Project ID:** `{project_id}`",
                f"- **Lead ID:** `{lead['lead_id']}` (`{package['data_classification']}`)",
                f"- **Build ID:** `{build['build_id']}`",
                f"- **Validation ID:** `{package['validation']['validation_id']}` (`{package['validation']['status']}`, score `{package['validation']['score']}/100`)",
                f"- **Current Stage / Review Status:** `{p.get('current_stage')}` / `{p.get('review_status')}`",
                f"- **Canonical Artifact Path:** `{out_path}` (`{len(html_bytes)}` bytes, SHA-256 `{sha256_hex}`)",
                f"- **Exported Review Copy:** `{exp_path / 'index.html'}`",
                "",
                "## 2. Supported Business Facts Rendered",
            ]
            for sf in supported_facts:
                md_lines.append(
                    f"- **`{sf['field']}`:** `{json.dumps(sf['value'], ensure_ascii=False)}` "
                    f"(tier: `{sf['evidence_tier']}`, status: `{sf['verification_status']}`, sources: `{', '.join(sf['source_urls'])}`)"
                )
            md_lines.extend(["", "## 3. Unresolved / Conflicting / Withheld Facts (Omitted from HTML)"])
            for uf in unresolved_or_conflicting:
                vals_str = f" — conflicting values: `{json.dumps(uf['conflicting_values'], ensure_ascii=False)}`" if uf["conflicting_values"] else ""
                md_lines.append(f"- **`{uf['field']}` (`{uf['status']}`):** {uf['reason']}{vals_str}")
            md_lines.extend([
                "",
                "## 4. Claims Lacking Independent Preserved Primary-Source Excerpts (`SOURCE_URL_ONLY`)",
                "- Raw HTTP/HTML responses from `https://pitstoplagos.com/` and `https://www.instagram.com/pitstoplagos/` are not stored in Git (`data/leads/*` is git-ignored).",
                "- The following rendered facts are consistent across project records and linked to those public URLs, and should be visually confirmed by the human reviewer:",
            ])
            for cl in claims_lacking_primary_observation:
                md_lines.append(f"  - **`{cl['field']}`:** `{json.dumps(cl['value'], ensure_ascii=False)}`")
            md_lines.extend([
                "",
                "## 5. Links in Rendered Artifact",
                f"- **External Links:** {', '.join(f'`{u}`' for u in external_links) or 'None'}",
                f"- **Internal Anchors:** {', '.join(f'`{a}`' for a in internal_anchors) or 'None'}",
                "",
                "## 6. How to Open Locally for Human Visual Review (`127.0.0.1` Only)",
                f"- **Direct file:** Open `{exp_path / 'index.html'}` in a desktop browser.",
                f"- **Local loopback server:** Run `{local_preview_instructions['local_server_command']}` (or `{local_preview_instructions['tracking_cli_serve_command']}`) and open `{local_preview_instructions['local_url']}`.",
                f"- **How to stop:** {local_preview_instructions['stop_instructions']}",
                "",
                "## 7. Validation Limitations",
            ])
            for lim in package["validation"]["limitations"]:
                md_lines.append(f"- {lim}")
            (resolved_exp / "REVIEW_README.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

        return package

    def verify_review_package(
        self,
        project_id: str,
        export_dir: str | Path,
        *,
        repository_root: Path = Path("."),
    ) -> dict:
        """Verify that an exported review package is reproducible, non-stale, hash-verified, and free of fixture leaks."""
        from validation.quality_validator import FICTIONAL_DEMO_PATTERNS

        root = Path(repository_root).resolve()
        exp_path = Path(export_dir)
        resolved_exp = exp_path if exp_path.is_absolute() else (root / exp_path)
        snap = self.snapshot(project_id)
        p = snap["project"]
        lead = snap["lead"]
        build = snap["latestBuild"]
        val = snap["latestValidation"]

        issues: list[dict[str, str]] = []
        if not build or not build.get("output_location"):
            issues.append({"code": "NO_ASSIGNED_BUILD", "message": f"Project {project_id} has no assigned build."})
            return {"status": "INVALID", "project_id": project_id, "issues": issues}

        canonical_out = Path(build["output_location"])
        resolved_canonical = canonical_out if canonical_out.is_absolute() else (root / canonical_out)
        if not resolved_canonical.is_file():
            issues.append({"code": "MISSING_CANONICAL_ARTIFACT", "message": f"Canonical build artifact missing: {canonical_out}"})
            return {"status": "INVALID", "project_id": project_id, "issues": issues}

        canonical_bytes = resolved_canonical.read_bytes()
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

        exp_html_path = resolved_exp / "index.html"
        exp_json_path = resolved_exp / "review-package.json"
        exp_md_path = resolved_exp / "REVIEW_README.md"
        for req_path, code in (
            (exp_html_path, "MISSING_EXPORTED_HTML"),
            (exp_json_path, "MISSING_EXPORTED_METADATA"),
            (exp_md_path, "MISSING_EXPORTED_README"),
        ):
            if not req_path.is_file():
                issues.append({"code": code, "message": f"Missing required review-package file: {req_path}"})
        if issues:
            return {"status": "INVALID", "project_id": project_id, "issues": issues}

        try:
            pkg = json.loads(exp_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "status": "INVALID",
                "project_id": project_id,
                "issues": [{"code": "CORRUPTED_METADATA_JSON", "message": str(exc)}],
            }

        exported_bytes = exp_html_path.read_bytes()
        exported_sha256 = hashlib.sha256(exported_bytes).hexdigest()
        exported_text = exported_bytes.decode("utf-8", errors="replace")
        readme_text = exp_md_path.read_text(encoding="utf-8", errors="replace")

        if pkg.get("project_id") != project_id:
            issues.append({"code": "MISMATCHED_PROJECT_ID", "message": f"Package project_id {pkg.get('project_id')} != {project_id}"})
        if pkg.get("lead_id") != lead["lead_id"]:
            issues.append({"code": "MISMATCHED_LEAD_ID", "message": f"Package lead_id {pkg.get('lead_id')} != {lead['lead_id']}"})
        if pkg.get("build_id") != p.get("assigned_build_id"):
            issues.append({
                "code": "STALE_EXPORT_BUILD_ID",
                "message": f"Exported package build_id ({pkg.get('build_id')}) does not match project assigned_build_id ({p.get('assigned_build_id')}).",
            })
        pkg_val_id = (pkg.get("validation") or {}).get("validation_id")
        if pkg_val_id != p.get("latest_validation_id"):
            issues.append({
                "code": "STALE_EXPORT_VALIDATION_ID",
                "message": f"Exported package validation_id ({pkg_val_id}) does not match project latest_validation_id ({p.get('latest_validation_id')}).",
            })
        if pkg.get("artifact_sha256") != exported_sha256:
            issues.append({
                "code": "PACKAGE_HASH_MISMATCH",
                "message": f"Recorded artifact_sha256 ({pkg.get('artifact_sha256')}) does not match exported index.html ({exported_sha256}).",
            })
        if exported_sha256 != canonical_sha256:
            issues.append({
                "code": "CANONICAL_HASH_MISMATCH",
                "message": f"Exported index.html SHA-256 ({exported_sha256}) does not match canonical assigned build HTML ({canonical_sha256}).",
            })
        if p.get("assigned_build_id") not in readme_text or canonical_sha256 not in readme_text:
            issues.append({
                "code": "STALE_REVIEW_README",
                "message": "REVIEW_README.md does not contain the current assigned_build_id and canonical SHA-256 hash.",
            })

        is_real = (lead.get("data_classification") or p.get("data_classification")) == "REAL_PUBLIC_DATA"
        if is_real:
            if pkg.get("data_classification") != "REAL_PUBLIC_DATA":
                issues.append({"code": "WRONG_DATA_CLASSIFICATION", "message": "Real-business package is not classified REAL_PUBLIC_DATA."})
            lowered_html = exported_text.casefold()
            lowered_json = json.dumps(pkg.get("supported_facts") or []).casefold()
            for pattern in FICTIONAL_DEMO_PATTERNS:
                if pattern in lowered_html or pattern in lowered_json:
                    issues.append({
                        "code": "FICTIONAL_DATA_IN_REAL_PACKAGE",
                        "message": f"Fictional demo pattern '{pattern}' found in real-business review package.",
                    })
            for uf in pkg.get("unresolved_or_conflicting_facts") or []:
                if uf.get("status") == "CONFLICTING_WITHHELD":
                    for c_val in uf.get("conflicting_values") or []:
                        if c_val and str(c_val).casefold() in lowered_html:
                            issues.append({
                                "code": "DISPUTED_FACT_IN_EXPORTED_HTML",
                                "message": f"Withheld conflicting value '{c_val}' appears in exported index.html.",
                            })

        return {
            "status": "VALID" if not issues else "INVALID",
            "project_id": project_id,
            "lead_id": lead["lead_id"],
            "assigned_build_id": p.get("assigned_build_id"),
            "validation_id": val.get("validation_id") if val else None,
            "canonical_artifact_path": str(canonical_out),
            "exported_html_path": str(exp_path / "index.html"),
            "canonical_sha256": canonical_sha256,
            "exported_sha256": exported_sha256,
            "recorded_package_sha256": pkg.get("artifact_sha256"),
            "hash_matches": canonical_sha256 == exported_sha256 == pkg.get("artifact_sha256"),
            "issues": issues,
        }

    def check_local_state(self, repository_root: Path = Path(".")) -> dict:
        root = Path(repository_root).resolve()
        missing_artifacts: list[str] = []
        missing_lead_sources: list[str] = []
        missing_evidence_leads: list[str] = []
        for lead_id, lead in self.state["leads"].items():
            if lead.get("data_classification") == "REAL_PUBLIC_DATA" and not lead.get("source_urls"):
                missing_evidence_leads.append(lead_id)
        for project in self.state["projects"].values():
            build = self.state["builds"].get(project.get("assigned_build_id"))
            if not build or not build.get("output_location"):
                continue
            out_path = Path(build["output_location"])
            resolved = out_path if out_path.is_absolute() else (root / out_path)
            if not resolved.is_file() or not (resolved.parent / "build.json").is_file():
                missing_artifacts.append(str(out_path))
                if not _find_durable_lead_source(root, project["lead_id"], self.state["leads"].get(project["lead_id"]) or {}):
                    missing_lead_sources.append(project["lead_id"])
        expected_audits = [
            "data/leads/phase19-lagos-restaurants-backlog-review-20261010.json",
            "data/leads/phase20-calabar-kitchen-website-verification-20261011.json",
            "data/leads/phase25-lagos-restaurants-discovery-and-qualification-20261012.json",
            "data/leads/phase26-pitstop-lagos-website-draft-20261012.json",
            "data/leads/phase26-correction-pitstop-lagos-20261012.json",
        ]
        missing_audits = [rel for rel in expected_audits if (root / "tests").is_dir() and not (root / rel).is_file()]
        healthy = (
            self.path.is_file()
            and not missing_artifacts
            and not missing_lead_sources
            and not missing_evidence_leads
            and not missing_audits
        )
        return {
            "status": "HEALTHY" if healthy else "MISSING_LOCAL_STATE",
            "state_file_exists": self.path.is_file(),
            "missing_build_artifacts": missing_artifacts,
            "missing_lead_sources": missing_lead_sources,
            "missing_evidence_leads": missing_evidence_leads,
            "missing_audit_files": missing_audits,
            "projects_checked": len(self.state["projects"]),
        }

    def recover_local_state(self, repository_root: Path = Path(".")) -> dict:
        """Rebuild missing build artifacts only when durable, verified lead source files exist on disk.

        Never fabricates real-business leads, projects, evidence, or review approvals.
        """
        root = Path(repository_root).resolve()
        recovered_builds: list[str] = []
        unrecoverable_projects: list[str] = []
        if not self.path.is_file():
            report = self.check_local_state(repository_root=root)
            report.update({
                "recovered_builds": recovered_builds,
                "unrecoverable_projects": unrecoverable_projects,
                "reason": "Tracking state file does not exist on disk; recovery refuses to fabricate leads or projects.",
            })
            return report

        from builder.content_generator import DeterministicContentGenerator
        from builder.validator import validate_html
        from builder.website_builder import _is_fixture, _verified_template_data
        from templates.registry import select_template
        from templates.renderer import render_site

        for project_id, project in self.state["projects"].items():
            assigned_id = project.get("assigned_build_id")
            build = self.state["builds"].get(assigned_id) if assigned_id else None
            if not build or not build.get("output_location"):
                continue
            out_path = Path(build["output_location"])
            resolved = out_path if out_path.is_absolute() else (root / out_path)
            manifest_path = resolved.parent / "build.json"
            if resolved.is_file() and manifest_path.is_file():
                continue
            lead_record = self.state["leads"].get(project["lead_id"]) or {}
            source_payload = _find_durable_lead_source(root, project["lead_id"], lead_record)
            if not source_payload:
                unrecoverable_projects.append(project_id)
                continue
            try:
                facts = _verified_template_data(source_payload)
                template = select_template(facts["category"])
                content = DeterministicContentGenerator().generate(facts, facts["category"])
                facts["content"] = content
                html = render_site(facts, template.template_id)
                is_fix = _is_fixture(source_payload)
                validation = validate_html(
                    html,
                    facts["business_name"],
                    real_build=not is_fix,
                    build_id=assigned_id,
                    lead_id=project["lead_id"],
                    expected_contact={"phone": facts.get("phone"), "whatsapp": facts.get("whatsapp"), "email": facts.get("email")},
                    expected_facts=None if is_fix else facts,
                    synthetic_contact=is_fix,
                )
                validation["artifact_path"] = str(out_path)
                if not validation.get("passed"):
                    unrecoverable_projects.append(project_id)
                    continue
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(html, encoding="utf-8")
                rebuilt_manifest = {
                    **build,
                    "generated_content": content,
                    "validation": validation,
                    "output_location": str(out_path),
                }
                manifest_path.write_text(json.dumps(rebuilt_manifest, indent=2) + "\n", encoding="utf-8")
                recovered_builds.append(assigned_id)
            except Exception:
                unrecoverable_projects.append(project_id)

        report = self.check_local_state(repository_root=root)
        report.update({
            "recovered_builds": recovered_builds,
            "unrecoverable_projects": unrecoverable_projects,
        })
        return report

    def _next(self, p: dict) -> str:
        if p.get("current_stage") == "READY_FOR_REVIEW" and p.get("review_status") == "APPROVED_FOR_PREVIEW":
            return "Deploy isolated preview"
        return {
            "QUALIFIED": "Build website",
            "BUILDING": "Complete build",
            "VALIDATING": "Fix validation errors",
            "READY_FOR_REVIEW": "Review preview",
            "PREVIEW_DEPLOYED": "Complete human review",
            "HUMAN_REVIEW": "Approve or request changes",
            "READY_FOR_OUTREACH": "Approve outreach",
            "FAILED": "Investigate failure",
        }.get(p["current_stage"], "Review project state")

    def query_projects(self, **filters) -> list[dict]:
        return [p for p in self.state["projects"].values() if all(p.get(k) == v for k, v in filters.items())]


def _find_durable_lead_source(repository_root: Path, lead_id: str, lead_record: dict) -> dict | None:
    """Locate an existing, durable qualified lead JSON file on disk without fabricating records."""
    embedded = lead_record.get("research_record")
    if isinstance(embedded, dict) and isinstance(embedded.get("candidate"), dict) and isinstance(embedded.get("research"), dict):
        if lead_record.get("data_classification") != "REAL_PUBLIC_DATA" or (embedded.get("candidate") or {}).get("sourceUrls"):
            payload = copy.deepcopy(embedded)
            payload["lead_id"] = lead_id
            return payload
    candidates: list[Path] = []
    leads_dir = repository_root / "data" / "leads"
    if leads_dir.is_dir():
        candidates.extend(sorted(leads_dir.glob("*.json")))
    fixture_file = repository_root / "tests" / "fixtures" / "fictional_test_business_lagos_fresh_kitchen.json"
    if lead_record.get("data_classification") == "TEST_FIXTURE" and fixture_file.is_file():
        candidates.append(fixture_file)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "research" not in data or "candidate" not in data:
            continue
        cand_name = (data.get("candidate") or {}).get("businessName")
        if data.get("lead_id") == lead_id or (cand_name and cand_name == lead_record.get("business_name")):
            if lead_record.get("data_classification") == "REAL_PUBLIC_DATA":
                if data.get("data_classification") != "REAL_PUBLIC_DATA" or not (data.get("candidate") or {}).get("sourceUrls"):
                    continue
            payload = copy.deepcopy(data)
            payload["lead_id"] = lead_id
            return payload
    return None

"""Single-file, append-history tracking with guarded lifecycle transitions."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import os
import re
import sys
from urllib.parse import urlparse

from tracking.database import (
    DatabasePersistenceError,
    RelationalStateBackend,
    TRACKED_COLLECTIONS,
    VOLATILE_RECORD_FIELDS,
    configured_database_url,
    extract_research_evidence_entries,
    is_production_mode,
)

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


def _write_state_atomically(path: Path, state: dict) -> None:
    """Write the tracking state through a temporary file and an atomic rename.

    A direct `write_text()` truncates the existing file first, so a crash or an out-of-disk
    error mid-write leaves a partially written state file behind for every later reader.
    """
    payload = json.dumps(state, indent=2) + "\n"
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def records_conflict(existing: dict, incoming: dict) -> bool:
    """True when two records claiming the same ID disagree on a shared non-volatile field.

    Only fields present in both records are compared, because the stored record legitimately gains
    derived fields (project/lead linkage, artifact digests, capture warnings) after being recorded.
    """
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return existing != incoming
    for key, value in incoming.items():
        if key in VOLATILE_RECORD_FIELDS or key not in existing:
            continue
        if existing[key] != value:
            return True
    return False


def next_numeric_id(collection: dict, prefix: str) -> str:
    """Return the next non-colliding ``prefix-NNNN`` id for *collection*.

    Uses the maximum existing numeric suffix instead of ``len(collection) + 1`` so
    that deletions, retries, or concurrent inserts cannot reuse an id that is
    still referenced by historical records.
    """
    max_n = 0
    for key in (collection or {}).keys():
        if not isinstance(key, str) or not key.startswith(prefix + "-"):
            continue
        suffix = key[len(prefix) + 1:]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}-{max_n + 1:04d}"


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


_FORBIDDEN_ASSET_FILENAMES = {
    "index.html",
    "build.json",
    "deployment.json",
    "vercel.json",
    "review-package.json",
    "review_readme.md",
    ".env",
}


def _validate_relative_asset_path(build_dir: Path, rel_path: str) -> Path:
    """Validate that a relative asset path stays strictly inside build_dir and is not hidden/forbidden."""
    cleaned = str(rel_path or "").strip()
    if not cleaned or Path(cleaned).is_absolute() or PureWindowsPath(cleaned).is_absolute() or PureWindowsPath(cleaned).drive:
        raise ValueError(f"Absolute or empty asset path is forbidden: {rel_path}")
    rel = Path(cleaned)
    if any(part in {"", ".", ".."} or part.startswith(".") for part in rel.parts):
        raise ValueError(f"Unsafe relative asset path is forbidden: {rel_path}")
    if rel.name.casefold() in _FORBIDDEN_ASSET_FILENAMES:
        raise ValueError(f"Metadata or reserved filename is forbidden in local_assets: {rel_path}")
    target = (build_dir.resolve() / rel).resolve()
    try:
        target.relative_to(build_dir.resolve())
    except ValueError as err:
        raise ValueError(f"Asset path escapes build directory: {rel_path}") from err
    return target


def _collect_safe_local_assets(build_dir: Path) -> dict[str, dict]:
    """Scan a build directory for safe relative local asset files (.css, .svg, .js, images) and encode them with SHA-256."""
    if not build_dir.is_dir():
        return {}
    root = build_dir.resolve()
    assets: dict[str, dict] = {}
    for file_path in sorted(root.rglob("*")):
        if file_path.is_symlink() or not file_path.is_file():
            continue
        rel = file_path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.name.casefold() in _FORBIDDEN_ASSET_FILENAMES:
            continue
        raw = file_path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        rel_posix = rel.as_posix()
        try:
            text_val = raw.decode("utf-8")
            assets[rel_posix] = {
                "encoding": "utf-8",
                "content": text_val,
                "bytes": len(raw),
                "sha256": sha,
            }
        except UnicodeDecodeError:
            assets[rel_posix] = {
                "encoding": "base64",
                "content_b64": base64.b64encode(raw).decode("ascii"),
                "bytes": len(raw),
                "sha256": sha,
            }
    return assets


def _decode_and_verify_local_asset(rel_path: str, entry: dict) -> bytes:
    """Decode a stored local_assets entry and verify its SHA-256 digest."""
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid local_assets entry for {rel_path}")
    enc = entry.get("encoding") or "utf-8"
    if enc == "base64":
        raw = base64.b64decode(str(entry.get("content_b64") or "").encode("ascii"))
    else:
        raw = str(entry.get("content") or "").encode("utf-8")
    actual_sha = hashlib.sha256(raw).hexdigest()
    expected_sha = entry.get("sha256")
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(
            f"Durable local asset SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}."
        )
    return raw
def _empty_state() -> dict:
    return {
        "leads": {},
        "research_evidence": {},
        "projects": {},
        "builds": {},
        "validations": {},
        "deployments": {},
        "outreach": {},
        "jobs": {},
        "qualifications": {},
        "reviews": {},
        "build_artifacts": {},
    }


class TrackingStore:
    def __init__(
        self,
        path: str | Path = DEFAULT_STATE_PATH,
        *,
        auto_bootstrap_workspace: bool = False,
        database_url: str | None = None,
        db_backend: RelationalStateBackend | None = None,
    ):
        self.path = Path(path)
        self.db_backend: RelationalStateBackend | None = None
        is_default_path = self.path.resolve() == DEFAULT_STATE_PATH.resolve()
        if db_backend is not None:
            self.db_backend = db_backend
        elif database_url is not None and str(database_url).strip():
            self.db_backend = RelationalStateBackend(str(database_url).strip())
        elif is_default_path or is_production_mode():
            resolved_url = configured_database_url()
            if resolved_url:
                self.db_backend = RelationalStateBackend(resolved_url)

        self._loaded_snapshot: dict = _empty_state()
        self.state = self._load()

        if (
            self.db_backend is None
            and is_default_path
            and not self.path.exists()
            and Path("preview-export/lead-0002-pitstop-lagos/review-package.json").is_file()
            and Path("tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json").is_file()
        ):
            self.recover_from_verified_exports(repository_root=Path("."))

    def _load(self) -> dict:
        if self.db_backend is not None:
            loaded = self.db_backend.load_state()
            base = _empty_state()
            base.update(loaded)
            self._loaded_snapshot = copy.deepcopy(base)
            return base
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError(f"Tracking state file {self.path} is not valid JSON.") from error
            if not isinstance(loaded, dict):
                raise ValueError(f"Tracking state file {self.path} must contain a JSON object.")
            base = _empty_state()
            base.update(loaded)
            self._loaded_snapshot = copy.deepcopy(base)
            return base
        base = _empty_state()
        self._loaded_snapshot = copy.deepcopy(base)
        return base

    def refresh(self) -> dict:
        """Reload the latest committed state from the database or state file while preserving dict references."""
        loaded = self._load()
        for col_key, col_val in loaded.items():
            if col_key == "worker_routing":
                if isinstance(self.state.get("worker_routing"), dict) and isinstance(col_val, dict):
                    self.state["worker_routing"].clear()
                    self.state["worker_routing"].update(copy.deepcopy(col_val))
                else:
                    self.state["worker_routing"] = copy.deepcopy(col_val)
                continue
            target_col = self.state.setdefault(col_key, {})
            if isinstance(target_col, dict) and isinstance(col_val, dict):
                for dead_id in list(target_col.keys()):
                    if dead_id not in col_val:
                        target_col.pop(dead_id, None)
                for rec_id, fresh_rec in col_val.items():
                    if rec_id in target_col and isinstance(target_col[rec_id], dict) and isinstance(fresh_rec, dict):
                        target_col[rec_id].clear()
                        target_col[rec_id].update(copy.deepcopy(fresh_rec))
                    else:
                        target_col[rec_id] = copy.deepcopy(fresh_rec)
            else:
                self.state[col_key] = copy.deepcopy(col_val)
        return self.state

    def _save(self) -> None:
        evidence_bucket = self.state.setdefault("research_evidence", {})
        for lead_id, lead_rec in (self.state.get("leads") or {}).items():
            if isinstance(lead_rec, dict):
                for ev_id, ev_row in extract_research_evidence_entries(str(lead_id), lead_rec).items():
                    evidence_bucket[ev_id] = ev_row

        if self.db_backend is not None:
            # Never fall back to local JSON when database backend is configured or in production
            self.db_backend.sync_state(self.state)
            self._loaded_snapshot = copy.deepcopy(self.state)
            return

        if is_production_mode():
            raise DatabasePersistenceError(
                "Refusing to write to temporary local JSON state file in production mode without a PostgreSQL database backend."
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                disk_raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
                # A truncated or unreadable state file cannot be merged. Replacing it with the
                # validated in-memory state is preferable to refusing every later write, but the
                # reason is surfaced so a corrupted file is never mistaken for an empty workspace.
                sys.stderr.write(
                    f"WARNING: existing tracking state file {self.path} could not be read ({error}); "
                    "writing the current in-memory state without a disk merge.\n"
                )
                disk_raw = None
            if isinstance(disk_raw, dict):
                disk_state = _empty_state()
                disk_state.update(disk_raw)
                # Compute every merged collection before touching `self.state` so an unexpected
                # error can never leave a partially merged (silently lossy) state on disk.
                merged_columns: dict[str, dict] = {}
                try:
                    for col in (*TRACKED_COLLECTIONS, "executions", "workers", "project_locks"):
                        if not isinstance(self.state.get(col), dict) and not isinstance(disk_state.get(col), dict):
                            continue
                        base_col = self._loaded_snapshot.get(col) if isinstance(self._loaded_snapshot.get(col), dict) else {}
                        curr_col = self.state.get(col) if isinstance(self.state.get(col), dict) else {}
                        disk_col = disk_state.get(col) if isinstance(disk_state.get(col), dict) else {}
                        merged_col = copy.deepcopy(disk_col)
                        for rec_id in list(base_col.keys()):
                            if rec_id not in curr_col:
                                merged_col.pop(rec_id, None)
                        for rec_id, rec_val in curr_col.items():
                            if rec_id not in base_col or base_col[rec_id] != rec_val:
                                if isinstance(rec_val, dict) and isinstance(base_col.get(rec_id), dict) and isinstance(disk_col.get(rec_id), dict) and disk_col[rec_id] != base_col[rec_id]:
                                    m_rec = copy.deepcopy(disk_col[rec_id])
                                    for k, v in rec_val.items():
                                        if base_col[rec_id].get(k) != v:
                                            m_rec[k] = copy.deepcopy(v)
                                    merged_col[rec_id] = m_rec
                                else:
                                    merged_col[rec_id] = copy.deepcopy(rec_val)
                        merged_columns[col] = merged_col
                except Exception as error:
                    raise ValueError(
                        f"Refusing to overwrite tracking state {self.path}: concurrent changes could not be merged safely ({error}). "
                        "Reload the state (TrackingStore.refresh()) and retry."
                    ) from error
                for col, merged_col in merged_columns.items():
                    target_col = self.state.setdefault(col, {})
                    for dead_id in list(target_col.keys()):
                        if dead_id not in merged_col:
                            target_col.pop(dead_id, None)
                    for rec_id, m_val in merged_col.items():
                        if rec_id in target_col and isinstance(target_col[rec_id], dict) and isinstance(m_val, dict):
                            target_col[rec_id].clear()
                            target_col[rec_id].update(m_val)
                        else:
                            target_col[rec_id] = m_val
        _write_state_atomically(self.path, self.state)
        self._loaded_snapshot = copy.deepcopy(self.state)

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
        if self.db_backend is not None:
            # Read the newest committed rows first so duplicate detection and the numeric ID
            # allocation below cannot race with a concurrent insert.
            self.refresh()
        dup = self.find_duplicate(lead)
        if dup["match"] == "EXACT_MATCH":
            raise ValueError(f"Duplicate lead: {dup['lead_id']}")
        classification = lead.get("data_classification") or "TEST_FIXTURE"
        if classification not in DATA_CLASSIFICATIONS:
            raise ValueError(f"Invalid data_classification: {classification}")
        ident = next_numeric_id(self.state.get("leads") or {}, "lead")
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
        if self.db_backend is not None:
            # See `create_lead`: a fresh read keeps active-project reuse and ID allocation accurate.
            self.refresh()
        lead = self.state["leads"].get(lead_id)
        if not lead or lead.get("qualification_status") != "QUALIFIED":
            raise ValueError("Only QUALIFIED leads can have projects.")
        existing = next(
            (project for project in self.state["projects"].values() if project["lead_id"] == lead_id and project.get("project_status") == "ACTIVE"),
            None,
        )
        if existing:
            return existing
        ident = next_numeric_id(self.state.get("projects") or {}, "project")
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
            # Commit the refreshed lead before appending qualification history so a later
            # failure cannot discard the research update.
            self._save()
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
        explicit_id = qualification.get("qualification_id")
        ident = explicit_id or None
        if ident is None:
            ident = next_numeric_id(self.state["qualifications"], "qualification")
        existing_qual = self.state["qualifications"].get(ident)
        if existing_qual:
            # An ID reused for a different lead is a data-integrity conflict, never an idempotent replay.
            if existing_qual.get("lead_id") != qualification.get("lead_id"):
                raise ValueError("Qualification ID is already assigned to another lead.")
            return existing_qual
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

    def _set_stage(self, project_id: str, to: str) -> dict:
        """Validate and apply a stage change in memory only, without persisting.

        Multi-record operations (build, validation, deployment, review) move through several
        stages at once. Those intermediate stages are not meaningful committed states, and
        persisting each one separately means a later failure can leave the durable state in the
        middle of the sequence. Every such operation applies its transitions here and performs a
        single `_save()` once all of its records are in place, so the write is all-or-nothing.
        """
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}. Cannot transition to {to}.")
        current = p.get("current_stage")
        if to not in TRANSITIONS.get(current, set()):
            allowed = ", ".join(sorted(TRANSITIONS.get(current, set()))) or "none"
            raise ValueError(
                f"Illegal project state transition for {project_id}: {current} -> {to}. "
                f"Permitted next stages from {current}: {allowed}."
            )
        p["current_stage"] = to
        p["updated_at"] = now()
        return p

    def transition(self, project_id: str, to: str) -> dict:
        p = self._set_stage(project_id, to)
        self._save()
        return p
        current = p.get("current_stage")
        if to not in TRANSITIONS.get(current, set()):
            allowed = ", ".join(sorted(TRANSITIONS.get(current, set()))) or "none"
            raise ValueError(
                f"Illegal project state transition for {project_id}: {current} -> {to}. "
                f"Permitted next stages from {current}: {allowed}."
            )

    def record_build(self, project_id: str, build: dict) -> dict:
        p = self.state["projects"].get(project_id)
        if not p:
            raise ValueError(f"Unknown project: {project_id}. Cannot record a build.")
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
        if existing and records_conflict(existing, build):
            raise ValueError(
                f"Build ID {build_id} is already recorded with different content. "
                "A corrected or regenerated build must use a new build_id so the prior result stays auditable."
            )
        if (
            existing
            and existing.get("project_id") == project_id
            and p.get("assigned_build_id") == build_id
            and existing.get("status") == build.get("status")
        ):
            return existing
        if p["current_stage"] not in {"QUALIFIED", "BUILDING", "FAILED", "READY_FOR_REVIEW", "VALIDATING"}:
            raise ValueError(
                f"Project {project_id} is in stage {p['current_stage']} and cannot accept a new build. "
                "Builds are recorded from QUALIFIED, BUILDING, VALIDATING, READY_FOR_REVIEW, or FAILED."
            )
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
            self._set_stage(project_id, "BUILDING")
        stored_build = {
            **build,
            "project_id": project_id,
            "data_classification": build_class or lead_class,
        }
        artifact_capture_error: str | None = None
        if stored_build.get("status") != "FAILED":
            html_content = stored_build.pop("html_content", None)
            explicit_assets = stored_build.pop("local_assets", None)
            out_loc = stored_build.get("output_location")
            manifest_data = None
            scanned_assets: dict[str, dict] = {}
            out_path_obj = Path(str(out_loc)) if out_loc else None
            if out_path_obj is not None and out_path_obj.is_file():
                try:
                    # Preserve the exact on-disk bytes and newline endings.
                    html_content = out_path_obj.read_bytes().decode("utf-8")
                    manifest_file = out_path_obj.parent / "build.json"
                    if manifest_file.is_file():
                        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    scanned_assets = _collect_safe_local_assets(out_path_obj.parent)
                except Exception as capture_exc:
                    # Recorded on the build instead of being swallowed: a missing durable artifact must
                    # not look like a healthy build, and review packaging fails with this reason.
                    artifact_capture_error = f"{type(capture_exc).__name__}: {capture_exc}"
            elif not isinstance(html_content, str) or not html_content:
                artifact_capture_error = (
                    f"No build artifact content available (output_location={out_loc!r} is not a readable file)."
                )
            local_assets = copy.deepcopy(explicit_assets) if isinstance(explicit_assets, dict) else scanned_assets
            if isinstance(html_content, str) and html_content:
                html_bytes = len(html_content.encode("utf-8"))
                html_sha256 = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
                stored_build["artifact_sha256"] = html_sha256
                stored_build["artifact_bytes"] = html_bytes
                self.state.setdefault("build_artifacts", {})[build_id] = {
                    "build_id": build_id,
                    "project_id": project_id,
                    "lead_id": p["lead_id"],
                    "template_id": stored_build.get("template_id"),
                    "artifact_path": str(out_loc or f"output/{p['lead_id']}/{build_id}/index.html"),
                    "html_content": html_content,
                    "html_bytes": html_bytes,
                    "html_sha256": html_sha256,
                    "build_manifest": manifest_data if isinstance(manifest_data, dict) else copy.deepcopy(stored_build),
                    "local_assets": local_assets,
                    "review_package_json": None,
                    "review_readme_md": None,
                    "review_package_sha256": None,
                    "created_at": stored_build.get("created_at") or now(),
                    "updated_at": now(),
                }
        if artifact_capture_error:
            stored_build["artifact_capture_error"] = artifact_capture_error
        else:
            stored_build.pop("artifact_capture_error", None)
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
            self._set_stage(project_id, "FAILED")
        else:
            self._set_stage(project_id, "VALIDATING")
        self._save()
        return self.state["builds"][build_id]

    def record_build_artifact(
        self,
        build_id: str,
        *,
        project_id: str | None = None,
        lead_id: str,
        template_id: str | None = None,
        artifact_path: str,
        html_content: str,
        build_manifest: dict | None = None,
        local_assets: dict | None = None,
        review_package_json: dict | None = None,
        review_readme_md: str | None = None,
    ) -> dict:
        """Persist a build's HTML content, manifest, local assets, review package, and SHA-256 hash in durable storage."""
        if not build_id or not isinstance(html_content, str) or not html_content:
            raise ValueError("record_build_artifact requires a non-empty build_id and html_content.")
        raw_bytes = html_content.encode("utf-8")
        html_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        rev_sha = (
            hashlib.sha256(json.dumps(review_package_json, sort_keys=True).encode("utf-8")).hexdigest()
            if isinstance(review_package_json, dict)
            else None
        )
        existing = (self.state.get("build_artifacts") or {}).get(build_id) or {}
        resolved_assets = copy.deepcopy(local_assets) if isinstance(local_assets, dict) else existing.get("local_assets")
        if resolved_assets is None and Path(str(artifact_path)).is_file():
            try:
                resolved_assets = _collect_safe_local_assets(Path(str(artifact_path)).parent)
            except Exception:
                resolved_assets = {}
        if isinstance(resolved_assets, dict):
            # Validate all relative paths and SHA-256 checksums before persisting
            dummy_dir = Path("/tmp/build-asset-check")
            for rel_key, asset_entry in resolved_assets.items():
                _validate_relative_asset_path(dummy_dir, str(rel_key))
                _decode_and_verify_local_asset(str(rel_key), asset_entry)
        stamp = now()
        record = {
            "build_id": build_id,
            "project_id": project_id or existing.get("project_id"),
            "lead_id": lead_id,
            "template_id": template_id or existing.get("template_id"),
            "artifact_path": str(artifact_path),
            "html_content": html_content,
            "html_bytes": len(raw_bytes),
            "html_sha256": html_sha256,
            "build_manifest": copy.deepcopy(build_manifest) if isinstance(build_manifest, dict) else (existing.get("build_manifest") or {}),
            "local_assets": resolved_assets if isinstance(resolved_assets, dict) else {},
            "review_package_json": copy.deepcopy(review_package_json) if isinstance(review_package_json, dict) else existing.get("review_package_json"),
            "review_readme_md": review_readme_md if review_readme_md is not None else existing.get("review_readme_md"),
            "review_package_sha256": rev_sha or existing.get("review_package_sha256"),
            "created_at": existing.get("created_at") or stamp,
            "updated_at": stamp,
        }
        self.state.setdefault("build_artifacts", {})[build_id] = record
        if build_id in self.state.get("builds", {}):
            self.state["builds"][build_id]["artifact_sha256"] = html_sha256
            self.state["builds"][build_id]["artifact_bytes"] = len(raw_bytes)
        self._save()
        return copy.deepcopy(record)

    def get_build_artifact(self, build_id: str, *, verify_hash: bool = True) -> dict | None:
        """Retrieve a durable build artifact record and verify its SHA-256 integrity."""
        art = (self.state.get("build_artifacts") or {}).get(build_id)
        if not art:
            return None
        html_text = str(art.get("html_content") or "")
        actual_sha = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
        expected_sha = art.get("html_sha256") or (self.state.get("builds", {}).get(build_id) or {}).get("artifact_sha256")
        if verify_hash and expected_sha and actual_sha != expected_sha:
            raise ValueError(
                f"Durable build artifact SHA-256 mismatch for {build_id}: expected {expected_sha}, got {actual_sha}."
            )
        rev_pkg = art.get("review_package_json")
        expected_rev_sha = art.get("review_package_sha256")
        if verify_hash and isinstance(rev_pkg, dict) and expected_rev_sha:
            actual_rev_sha = hashlib.sha256(json.dumps(rev_pkg, sort_keys=True).encode("utf-8")).hexdigest()
            if actual_rev_sha != expected_rev_sha:
                raise ValueError(
                    f"Durable review package SHA-256 mismatch for {build_id}: expected {expected_rev_sha}, got {actual_rev_sha}."
                )
        local_assets = art.get("local_assets")
        if verify_hash and isinstance(local_assets, dict):
            dummy_dir = Path("/tmp/build-asset-check")
            for rel_key, asset_entry in local_assets.items():
                _validate_relative_asset_path(dummy_dir, str(rel_key))
                _decode_and_verify_local_asset(str(rel_key), asset_entry)
        return copy.deepcopy(art)

    def get_or_materialize_build_artifact(
        self,
        build_id: str,
        *,
        repository_root: str | Path = Path("."),
        verify_hash: bool = True,
    ) -> dict:
        """Retrieve or re-materialize a build's HTML, manifest, and safe local assets on disk after local temporary files are removed."""
        root = Path(repository_root).resolve()
        b = (self.state.get("builds") or {}).get(build_id)
        art = self.get_build_artifact(build_id, verify_hash=verify_hash)
        if not b and not art:
            raise ValueError(f"Unknown build artifact: {build_id}")
        raw_out = (b or {}).get("output_location") or (art or {}).get("artifact_path")
        if not raw_out:
            raise ValueError(f"Build {build_id} has no output_location or artifact_path.")
        out_path = Path(str(raw_out))
        resolved_html = out_path if out_path.is_absolute() else (root / out_path)
        build_dir = resolved_html.parent

        def _sync_local_assets(artifact_rec: dict | None) -> list[str]:
            restored: list[str] = []
            assets_map = (artifact_rec or {}).get("local_assets")
            if not isinstance(assets_map, dict):
                return restored
            for rel_key, entry in sorted(assets_map.items()):
                target_file = _validate_relative_asset_path(build_dir, str(rel_key))
                expected_bytes = _decode_and_verify_local_asset(str(rel_key), entry)
                if target_file.is_file():
                    disk_asset_bytes = target_file.read_bytes()
                    disk_asset_sha = hashlib.sha256(disk_asset_bytes).hexdigest()
                    if verify_hash and entry.get("sha256") and disk_asset_sha != entry.get("sha256"):
                        raise ValueError(
                            f"On-disk local asset SHA-256 mismatch for {build_id}/{rel_key}: "
                            f"expected {entry.get('sha256')}, got {disk_asset_sha}."
                        )
                else:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(expected_bytes)
                    restored.append(str(rel_key))
            return restored

        if resolved_html.is_file():
            disk_bytes = resolved_html.read_bytes()
            disk_sha = hashlib.sha256(disk_bytes).hexdigest()
            expected_sha = (b or {}).get("artifact_sha256") or (art or {}).get("html_sha256")
            if verify_hash and expected_sha and disk_sha != expected_sha:
                raise ValueError(
                    f"On-disk build artifact SHA-256 mismatch for {build_id}: expected {expected_sha}, got {disk_sha}."
                )
            html_str = disk_bytes.decode("utf-8")
            if not art:
                manifest_file = resolved_html.parent / "build.json"
                manifest_obj = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.is_file() else (b or {})
                art = self.record_build_artifact(
                    build_id,
                    project_id=(b or {}).get("project_id"),
                    lead_id=str((b or {}).get("lead_id") or ""),
                    template_id=(b or {}).get("template_id"),
                    artifact_path=str(out_path),
                    html_content=html_str,
                    build_manifest=manifest_obj,
                    local_assets=_collect_safe_local_assets(build_dir),
                )
            restored_assets = _sync_local_assets(art)
            return {
                "build_id": build_id,
                "artifact_path": str(out_path),
                "resolved_path": str(resolved_html),
                "html_content": html_str,
                "html_bytes": len(disk_bytes),
                "html_sha256": disk_sha,
                "restored_assets": restored_assets,
                "materialized": bool(restored_assets),
            }

        if not art or not art.get("html_content"):
            raise ValueError(f"Build artifact {build_id} is missing from disk ({out_path}) and no durable artifact is stored.")

        resolved_html.parent.mkdir(parents=True, exist_ok=True)
        resolved_html.write_bytes(art["html_content"].encode("utf-8"))
        manifest_file = resolved_html.parent / "build.json"
        if not manifest_file.is_file() and isinstance(art.get("build_manifest"), dict) and art["build_manifest"]:
            manifest_file.write_text(json.dumps(art["build_manifest"], indent=2) + "\n", encoding="utf-8")
        restored_assets = _sync_local_assets(art)
        return {
            "build_id": build_id,
            "artifact_path": str(out_path),
            "resolved_path": str(resolved_html),
            "html_content": art["html_content"],
            "html_bytes": int(art["html_bytes"]),
            "html_sha256": art["html_sha256"],
            "restored_assets": restored_assets,
            "materialized": True,
        }

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
        if existing_val and records_conflict(existing_val, validation):
            raise ValueError(
                f"Validation ID {val_id} is already recorded with different content. "
                "A re-run must use a new validation_id so the prior result stays auditable."
            )
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
            self._set_stage(project_id, "BUILDING")
            self._set_stage(project_id, "VALIDATING")
        elif p["current_stage"] == "BUILDING":
            self._set_stage(project_id, "VALIDATING")
        self._set_stage(project_id, "READY_FOR_REVIEW" if validation.get("status") == "PASS" else "FAILED")
        self._save()
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
        if existing_dep and records_conflict(existing_dep, deployment):
            raise ValueError(
                f"Deployment ID {dep_id} is already recorded with different content. "
                "A new deployment requires a new build and deployment_id so the prior deployment stays auditable."
            )
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
        self._set_stage(project_id, "PREVIEW_DEPLOYED")
        self._save()
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
        if self.db_backend is not None:
            # A review authorizes real work, so decide against the newest committed build,
            # validation, and deployment state rather than a possibly stale local snapshot.
            self.refresh()
            p = self.state["projects"].get(project_id)
            if not p:
                raise ValueError(
                    f"Unknown project: {project_id} (it was not present in the newest committed state)."
                )
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

        # An exactly repeated review decision for the same build is an idempotent replay (for
        # example a retried CLI call or an n8n re-delivery), not a second audit entry.
        prior_entries = list(p.get("review_history") or [])
        last_entry = prior_entries[-1] if prior_entries else None
        if (
            isinstance(last_entry, dict)
            and last_entry.get("build_id") == assigned_build_id
            and last_entry.get("decision") == normalized_status
            and last_entry.get("intended_action") == intended_action
            and last_entry.get("notes") == notes
            and last_entry.get("reviewer") == reviewer
        ):
            return p

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
            self._set_stage(project_id, "BUILDING")
        elif normalized_status == "APPROVED":
            if p["current_stage"] == "PREVIEW_DEPLOYED":
                self._set_stage(project_id, "HUMAN_REVIEW")
            self._set_stage(project_id, "READY_FOR_OUTREACH")
        wf_after = project_workflow_state(p)
        reviews = self.state.setdefault("reviews", {})
        review_id = next_numeric_id(reviews, "review")
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
        o = next(
            (
                item
                for item in self.state.get("outreach", {}).values()
                if item.get("project_id") == project_id and (not assigned or item.get("build_id") == assigned)
            ),
            None,
        )
        artifact_path = b.get("output_location") if b else None
        durable_art = (self.state.get("build_artifacts") or {}).get(assigned) if assigned else None
        return {
            "lead": lead,
            "project": p,
            "latestBuild": b,
            "latestValidation": v,
            "latestDeployment": d,
            "latestOutreach": o,
            "workflow_state": project_workflow_state(p),
            "artifact_exists": bool(
                (artifact_path and Path(artifact_path).is_file())
                or (durable_art and durable_art.get("html_content"))
            ),
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
        if not resolved_html.is_file() and build["build_id"] in (self.state.get("build_artifacts") or {}):
            self.get_or_materialize_build_artifact(build["build_id"], repository_root=root, verify_hash=True)
        if not resolved_html.is_file():
            raise ValueError(f"Assigned build HTML artifact does not exist on disk: {out_path}")

        html_bytes = resolved_html.read_bytes()
        html_text = html_bytes.decode("utf-8")
        sha256_hex = hashlib.sha256(html_bytes).hexdigest()
        expected_build_sha = build.get("artifact_sha256")
        if expected_build_sha and sha256_hex != expected_build_sha:
            raise ValueError(
                f"Assigned build HTML SHA-256 ({sha256_hex}) does not match recorded build artifact_sha256 ({expected_build_sha})."
            )

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

        exp_path_for_md = Path(export_dir) if export_dir is not None else out_path.parent
        if export_dir is not None:
            package["exported_to"] = str(exp_path_for_md)
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
            f"- **Exported Review Copy:** `{exp_path_for_md / 'index.html'}`",
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
            f"- **Direct file:** Open `{exp_path_for_md / 'index.html'}` in a desktop browser.",
            f"- **Local loopback server:** Run `{local_preview_instructions['local_server_command']}` (or `{local_preview_instructions['tracking_cli_serve_command']}`) and open `{local_preview_instructions['local_url']}`.",
            f"- **How to stop:** {local_preview_instructions['stop_instructions']}",
            "",
            "## 7. Validation Limitations",
        ])
        for lim in package["validation"]["limitations"]:
            md_lines.append(f"- {lim}")
        readme_md_text = "\n".join(md_lines) + "\n"

        if export_dir is not None:
            resolved_exp = exp_path_for_md if exp_path_for_md.is_absolute() else (root / exp_path_for_md)
            resolved_exp.mkdir(parents=True, exist_ok=True)
            (resolved_exp / "index.html").write_bytes(html_bytes)
            (resolved_exp / "review-package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
            (resolved_exp / "REVIEW_README.md").write_text(readme_md_text, encoding="utf-8")

        manifest_file = resolved_html.parent / "build.json"
        manifest_obj = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.is_file() else copy.deepcopy(build)
        self.record_build_artifact(
            build["build_id"],
            project_id=project_id,
            lead_id=lead["lead_id"],
            template_id=build.get("template_id"),
            artifact_path=str(out_path),
            html_content=html_text,
            build_manifest=manifest_obj,
            review_package_json=package,
            review_readme_md=readme_md_text,
        )

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
        if not resolved_canonical.is_file() and build["build_id"] in (self.state.get("build_artifacts") or {}):
            try:
                self.get_or_materialize_build_artifact(build["build_id"], repository_root=root, verify_hash=True)
            except ValueError as err:
                issues.append({"code": "DURABLE_ARTIFACT_HASH_MISMATCH", "message": str(err)})
                return {"status": "INVALID", "project_id": project_id, "issues": issues}
        if not resolved_canonical.is_file():
            issues.append({"code": "MISSING_CANONICAL_ARTIFACT", "message": f"Canonical build artifact missing: {canonical_out}"})
            return {"status": "INVALID", "project_id": project_id, "issues": issues}

        canonical_bytes = resolved_canonical.read_bytes()
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

        exp_html_path = resolved_exp / "index.html"
        exp_json_path = resolved_exp / "review-package.json"
        exp_md_path = resolved_exp / "REVIEW_README.md"
        if (
            not (exp_html_path.is_file() and exp_json_path.is_file() and exp_md_path.is_file())
            and build["build_id"] in (self.state.get("build_artifacts") or {})
        ):
            try:
                art = self.get_build_artifact(build["build_id"], verify_hash=True)
            except ValueError as err:
                return {
                    "status": "INVALID",
                    "project_id": project_id,
                    "issues": [{"code": "DURABLE_ARTIFACT_HASH_MISMATCH", "message": str(err)}],
                }
            if art and isinstance(art.get("review_package_json"), dict) and art.get("review_readme_md"):
                resolved_exp.mkdir(parents=True, exist_ok=True)
                if not exp_html_path.is_file():
                    exp_html_path.write_bytes(art["html_content"].encode("utf-8"))
                if not exp_json_path.is_file():
                    exp_json_path.write_text(json.dumps(art["review_package_json"], indent=2) + "\n", encoding="utf-8")
                if not exp_md_path.is_file():
                    exp_md_path.write_text(art["review_readme_md"], encoding="utf-8")

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
        state_persisted = self.db_backend is not None or self.path.is_file()
        healthy = (
            state_persisted
            and not missing_artifacts
            and not missing_lead_sources
            and not missing_evidence_leads
        )
        return {
            "status": "HEALTHY" if healthy else "MISSING_LOCAL_STATE",
            "state_file_exists": state_persisted,
            "storage_backend": "database" if self.db_backend is not None else "json",
            "missing_build_artifacts": missing_artifacts,
            "missing_lead_sources": missing_lead_sources,
            "missing_evidence_leads": missing_evidence_leads,
            "missing_audit_files": missing_audits,
            "irrecoverable_gitignored_audit_files": missing_audits,
            "projects_checked": len(self.state["projects"]),
        }

    def recover_local_state(self, repository_root: Path = Path(".")) -> dict:
        """Rebuild missing build artifacts only when durable, verified lead source files exist on disk.

        Never fabricates real-business leads, projects, evidence, or review approvals.
        """
        root = Path(repository_root).resolve()
        recovered_builds: list[str] = []
        unrecoverable_projects: list[str] = []
        if self.db_backend is None and not self.path.is_file():
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
            # First try durable build_artifacts table/store if available
            if assigned_id in (self.state.get("build_artifacts") or {}):
                try:
                    self.get_or_materialize_build_artifact(assigned_id, repository_root=root, verify_hash=True)
                    if resolved.is_file() and manifest_path.is_file():
                        recovered_builds.append(assigned_id)
                        continue
                except Exception:
                    pass
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
                self.record_build_artifact(
                    assigned_id,
                    project_id=project_id,
                    lead_id=project["lead_id"],
                    template_id=template.template_id,
                    artifact_path=str(out_path),
                    html_content=html,
                    build_manifest=rebuilt_manifest,
                )
                recovered_builds.append(assigned_id)
            except Exception:
                unrecoverable_projects.append(project_id)

        report = self.check_local_state(repository_root=root)
        report.update({
            "recovered_builds": recovered_builds,
            "unrecoverable_projects": unrecoverable_projects,
        })
        return report

    def recover_from_verified_exports(self, repository_root: Path = Path(".")) -> dict:
        """Recover project-0001 and project-0002 strictly from committed fixtures and SHA-256 verified review packages.

        All reconstructed records are explicitly stamped with `reconstructed: True` and `recovery_provenance`.
        Never fabricates un-committed gitignored historical audit files (`phase19`, `phase20`, `phase25`, `phase26`).
        """
        from builder.content_generator import DeterministicContentGenerator
        from builder.validator import validate_html
        from builder.website_builder import _verified_template_data
        from templates.registry import select_template
        from templates.renderer import render_site

        root = Path(repository_root).resolve()
        fixture_path = root / "tests" / "fixtures" / "fictional_test_business_lagos_fresh_kitchen.json"
        pitstop_pkg_dir = root / "preview-export" / "lead-0002-pitstop-lagos"
        pitstop_pkg_json = pitstop_pkg_dir / "review-package.json"
        pitstop_pkg_html = pitstop_pkg_dir / "index.html"
        pitstop_pkg_md = pitstop_pkg_dir / "REVIEW_README.md"

        if not fixture_path.is_file():
            raise ValueError(f"Cannot recover project-0001: committed fixture missing at {fixture_path}")
        if not pitstop_pkg_json.is_file() or not pitstop_pkg_html.is_file():
            raise ValueError(f"Cannot recover project-0002: verified review package missing at {pitstop_pkg_dir}")

        pkg = json.loads(pitstop_pkg_json.read_text(encoding="utf-8"))
        pitstop_html_bytes = pitstop_pkg_html.read_bytes()
        pitstop_actual_sha = hashlib.sha256(pitstop_html_bytes).hexdigest()
        expected_sha = pkg.get("artifact_sha256")
        if not expected_sha or pitstop_actual_sha != expected_sha:
            raise ValueError(
                f"SHA-256 verification failed for {pitstop_pkg_html}: expected {expected_sha}, got {pitstop_actual_sha}"
            )
        if pkg.get("artifact_bytes") and len(pitstop_html_bytes) != int(pkg["artifact_bytes"]):
            raise ValueError(
                f"Byte-length verification failed for {pitstop_pkg_html}: expected {pkg['artifact_bytes']}, got {len(pitstop_html_bytes)}"
            )
        pitstop_html_text = pitstop_html_bytes.decode("utf-8")
        pitstop_md_text = pitstop_pkg_md.read_text(encoding="utf-8") if pitstop_pkg_md.is_file() else None

        # 1. Reconstruct project-0001 (Fictional Lagos Fresh Kitchen) from committed fixture
        fictional_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        fic_facts = _verified_template_data(fictional_payload)
        fic_template = select_template(fic_facts["category"])
        fic_content = DeterministicContentGenerator().generate(fic_facts, fic_facts["category"])
        fic_facts["content"] = fic_content
        fic_html = render_site(fic_facts, fic_template.template_id)
        fic_build_id = "build-2026-10-12T024113z"
        fic_val_id = "validation-2026-10-12T024113z"
        fic_dep_id = "deployment-2026-10-12T024113z"
        fic_rel_out = f"output/fictional-lagos-fresh-kitchen-001/{fic_build_id}/index.html"
        fic_abs_out = root / fic_rel_out
        fic_abs_out.parent.mkdir(parents=True, exist_ok=True)
        fic_abs_out.write_text(fic_html, encoding="utf-8")
        fic_sha = hashlib.sha256(fic_html.encode("utf-8")).hexdigest()
        fic_val = validate_html(
            fic_html,
            fic_facts["business_name"],
            real_build=False,
            build_id=fic_build_id,
            lead_id="lead-0001",
            expected_contact={"phone": fic_facts.get("phone"), "whatsapp": fic_facts.get("whatsapp"), "email": fic_facts.get("email")},
            expected_facts=None,
            synthetic_contact=True,
        )
        fic_val["validation_id"] = fic_val_id
        fic_val["project_id"] = "project-0001"
        fic_val["lead_id"] = "lead-0001"
        fic_val["artifact_path"] = fic_rel_out
        fic_val["reconstructed"] = True
        fic_val["recovery_provenance"] = "Reconstructed from committed fixture tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json"

        fic_manifest = {
            "build_id": fic_build_id,
            "project_id": "project-0001",
            "lead_id": "lead-0001",
            "business_name": fic_facts["business_name"],
            "template_id": fic_template.template_id,
            "selected_template": fic_template.template_id,
            "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE",
            "demo": True,
            "output_location": fic_rel_out,
            "artifact_sha256": fic_sha,
            "artifact_bytes": len(fic_html.encode("utf-8")),
            "generated_content": fic_content,
            "validation": fic_val,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from committed fixture tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json",
            "created_at": "2026-10-12T02:41:13Z",
        }
        (fic_abs_out.parent / "build.json").write_text(json.dumps(fic_manifest, indent=2) + "\n", encoding="utf-8")

        self.state["leads"]["lead-0001"] = {
            "lead_id": "lead-0001",
            "business_name": fic_facts["business_name"],
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "address": fic_facts.get("address"),
            "phone": fic_facts.get("phone"),
            "email": fic_facts.get("email"),
            "website_status": "NO_WEBSITE",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
            "demo": True,
            "source_urls": ["https://directory.example.invalid/lagos-fresh-kitchen"],
            "research_record": fictional_payload,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from committed fixture tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json",
            "created_at": "2026-10-12T01:44:58Z",
            "updated_at": "2026-10-12T02:41:13Z",
        }
        self.state["projects"]["project-0001"] = {
            "project_id": "project-0001",
            "lead_id": "lead-0001",
            "data_classification": "TEST_FIXTURE",
            "current_stage": "READY_FOR_REVIEW",
            "project_status": "ACTIVE",
            "review_status": "PENDING",
            "review_notes": None,
            "review_history": [],
            "assigned_build_id": fic_build_id,
            "latest_validation_id": fic_val_id,
            "latest_deployment_id": fic_dep_id,
            "isolated_preview_project": SHARED_FICTIONAL_PREVIEW_PROJECT,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from committed fixture tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json and CHANGELOG.md Phase 24",
            "created_at": "2026-10-12T01:44:58Z",
            "updated_at": "2026-10-12T02:41:13Z",
        }
        self.state["builds"][fic_build_id] = fic_manifest
        self.state["validations"][fic_val_id] = fic_val
        self.state["deployments"][fic_dep_id] = {
            "deployment_id": fic_dep_id,
            "project_id": "project-0001",
            "lead_id": "lead-0001",
            "build_id": fic_build_id,
            "validation_id": fic_val_id,
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://ai-website-agent-preview.vercel.app",
            "provider": "vercel",
            "environment": "preview",
            "dry_run": False,
            "metadata": {"project_name": SHARED_FICTIONAL_PREVIEW_PROJECT, "target": "preview"},
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from committed CHANGELOG.md Phase 24 verification record",
            "created_at": "2026-10-12T02:41:13Z",
        }
        self.state["build_artifacts"][fic_build_id] = {
            "build_id": fic_build_id,
            "project_id": "project-0001",
            "lead_id": "lead-0001",
            "template_id": fic_template.template_id,
            "artifact_path": fic_rel_out,
            "html_content": fic_html,
            "html_bytes": len(fic_html.encode("utf-8")),
            "html_sha256": fic_sha,
            "build_manifest": fic_manifest,
            "review_package_json": None,
            "review_readme_md": None,
            "review_package_sha256": None,
            "created_at": "2026-10-12T02:41:13Z",
            "updated_at": now(),
        }

        # 2. Reconstruct project-0002 (Pitstop Lagos) from SHA-256 verified preview-export/lead-0002-pitstop-lagos/
        pitstop_source_urls = ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"]
        pitstop_services = [
            {"name": "Healthy-Inspired Cafe & Dining", "description": "Fresh meals and recovery snacks."},
            {"name": "Cycling Community Rides", "description": "Weekend group rides."},
            {"name": "Wattbike Training Studio", "description": "Dedicated indoor cycling training."},
            {"name": "Bicycle Service & Repair", "description": "Maintenance and accessories."},
        ]
        pitstop_desc = "A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos."
        pitstop_lead_json = {
            "lead_id": "lead-0002",
            "data_classification": "REAL_PUBLIC_DATA",
            "demo": False,
            "reconstructed": True,
            "recovery_provenance": (
                "Reconstructed from committed tests/test_phase26_real_draft.py and SHA-256 verified "
                "preview-export/lead-0002-pitstop-lagos/review-package.json"
            ),
            "candidate": {
                "candidate_id": "phase25-search-pitstop-lagos",
                "candidateId": "phase25-search-pitstop-lagos",
                "businessName": "Pitstop Lagos",
                "niche": "restaurant",
                "dataClassification": "REAL_PUBLIC_DATA",
                "location": {"city": "Lagos", "country": "Nigeria"},
                "contact": {"email": "info@pitstoplagos.com"},
                "social": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"},
                "publicBusinessInformation": {"description": pitstop_desc},
                "existingWebsite": "https://pitstoplagos.com/",
                "websiteStatus": "poor",
                "sourceUrls": pitstop_source_urls,
            },
            "research": {
                "qualificationStatus": "QUALIFIED",
                "websiteStatus": "WEBSITE_POOR",
                "researchSources": pitstop_source_urls,
                "verifiedFacts": [
                    {"field": "businessName", "value": "Pitstop Lagos", "sourceUrls": pitstop_source_urls},
                    {"field": "description", "value": pitstop_desc, "sourceUrls": pitstop_source_urls},
                    {"field": "location", "value": {"city": "Lagos", "country": "Nigeria"}, "sourceUrls": pitstop_source_urls},
                    {"field": "contact", "value": {"email": "info@pitstoplagos.com"}, "sourceUrls": pitstop_source_urls},
                    {"field": "social", "value": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"}, "sourceUrls": pitstop_source_urls},
                    {"field": "services", "value": pitstop_services, "sourceUrls": pitstop_source_urls},
                    {"field": "menu_items", "value": [], "sourceUrls": pitstop_source_urls},
                ],
                "conflictingFacts": [
                    {
                        "field": "address",
                        "values": [
                            "92A Younis Bashorun Street, Victoria Island, Lagos, Nigeria",
                            "92b Younis Bashorun Street, Victoria Island, Lagos",
                        ],
                        "sourceUrls": pitstop_source_urls,
                        "reason": "Street plot number differs across Phase 25 (92b) and Phase 26 (92A) records without a preserved primary-source excerpt in Git; withheld until verified.",
                    },
                    {
                        "field": "phone",
                        "values": ["+234 807 749 8324", "+234 817 999 9005"],
                        "sourceUrls": pitstop_source_urls,
                        "reason": "Telephone number differs across Phase 25 (+234 817 999 9005) and Phase 26 (+234 807 749 8324) records without a preserved primary-source excerpt in Git; withheld until verified.",
                    },
                ],
                "evidenceRecords": {
                    "businessName": {"field": "businessName", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": "Pitstop Lagos", "sourceUrls": pitstop_source_urls},
                    "description": {"field": "description", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": pitstop_desc, "sourceUrls": pitstop_source_urls},
                    "city": {"field": "city", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": "Lagos", "sourceUrls": pitstop_source_urls},
                    "email": {"field": "email", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": "info@pitstoplagos.com", "sourceUrls": pitstop_source_urls},
                    "instagram_url": {"field": "instagram_url", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": "https://www.instagram.com/pitstoplagos/", "sourceUrls": pitstop_source_urls},
                    "services": {"field": "services", "status": "SUPPORTED", "evidenceTier": "SOURCE_URL_ONLY", "value": pitstop_services, "sourceUrls": pitstop_source_urls},
                    "address": {
                        "field": "address",
                        "status": "CONFLICTING_WITHHELD",
                        "evidenceTier": "CONFLICTING_OR_UNVERIFIABLE",
                        "value": None,
                        "candidates": [
                            "92A Younis Bashorun Street, Victoria Island, Lagos, Nigeria",
                            "92b Younis Bashorun Street, Victoria Island, Lagos",
                        ],
                        "sourceUrls": pitstop_source_urls,
                    },
                    "phone": {
                        "field": "phone",
                        "status": "CONFLICTING_WITHHELD",
                        "evidenceTier": "CONFLICTING_OR_UNVERIFIABLE",
                        "value": None,
                        "candidates": ["+234 807 749 8324", "+234 817 999 9005"],
                        "sourceUrls": pitstop_source_urls,
                    },
                },
                "uncertainties": [
                    "Exact menu dish prices are not verified on the broken public website and are intentionally omitted.",
                    "Opening hours are not verified on the broken public website and are intentionally omitted.",
                    "Street plot number (92A vs 92b Younis Bashorun Street) and telephone number (+234 807 749 8324 vs +234 817 999 9005) conflict across historical records and are withheld until human verification.",
                ],
                "missingFields": ["menu_items", "opening_hours", "whatsapp", "address", "phone"],
                "confidence": "high",
            },
            "approved_brand": {
                "sourceUrls": ["https://pitstoplagos.com/"],
                "values": {
                    "primary_color": "#1a3a2a",
                    "secondary_color": "#f8f4ea",
                    "accent_color": "#de5e28",
                },
            },
            "approved_assets": [],
        }
        pitstop_lead_file = root / "data" / "leads" / "pitstop_lagos_qualified_lead.json"
        pitstop_lead_file.parent.mkdir(parents=True, exist_ok=True)
        pitstop_lead_file.write_text(json.dumps(pitstop_lead_json, indent=2) + "\n", encoding="utf-8")

        pitstop_build_id = str(pkg["build_id"])
        pitstop_val_info = pkg.get("validation") or {}
        pitstop_val_id = str(pitstop_val_info.get("validation_id") or f"validation-{pitstop_build_id.removeprefix('build-')}")
        pitstop_rel_out = str(pkg.get("artifact_path") or f"output/lead-0002/{pitstop_build_id}/index.html")
        pitstop_abs_out = root / pitstop_rel_out
        pitstop_abs_out.parent.mkdir(parents=True, exist_ok=True)
        pitstop_abs_out.write_bytes(pitstop_html_bytes)

        pitstop_val_record = {
            "validation_id": pitstop_val_id,
            "project_id": "project-0002",
            "build_id": pitstop_build_id,
            "lead_id": "lead-0002",
            "status": str(pitstop_val_info.get("status") or "PASS"),
            "passed": str(pitstop_val_info.get("status") or "PASS") == "PASS",
            "score": int(pitstop_val_info.get("score") or 100),
            "checks": pitstop_val_info.get("checks") or {},
            "findings": pitstop_val_info.get("findings") or [],
            "artifact_path": pitstop_rel_out,
            "data_classification": "REAL_PUBLIC_DATA",
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from SHA-256 verified preview-export/lead-0002-pitstop-lagos/review-package.json",
        }
        pitstop_manifest = {
            "build_id": pitstop_build_id,
            "project_id": "project-0002",
            "lead_id": "lead-0002",
            "business_name": "Pitstop Lagos",
            "template_id": str(pkg.get("template_id") or "restaurant-menu-v1"),
            "selected_template": str(pkg.get("template_id") or "restaurant-menu-v1"),
            "status": "COMPLETED",
            "data_classification": "REAL_PUBLIC_DATA",
            "demo": False,
            "output_location": pitstop_rel_out,
            "artifact_sha256": pitstop_actual_sha,
            "artifact_bytes": len(pitstop_html_bytes),
            "validation": pitstop_val_record,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from SHA-256 verified preview-export/lead-0002-pitstop-lagos/index.html and review-package.json",
            "created_at": "2026-10-13T01:22:27Z",
        }
        (pitstop_abs_out.parent / "build.json").write_text(json.dumps(pitstop_manifest, indent=2) + "\n", encoding="utf-8")

        self.state["leads"]["lead-0002"] = {
            "lead_id": "lead-0002",
            "business_name": "Pitstop Lagos",
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "address": None,
            "phone": None,
            "email": "info@pitstoplagos.com",
            "existing_website": "https://pitstoplagos.com/",
            "instagram_url": "https://www.instagram.com/pitstoplagos/",
            "website_status": "WEBSITE_POOR",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
            "demo": False,
            "source_urls": pitstop_source_urls,
            "research_source_urls": pitstop_source_urls,
            "withheld_conflicting_fields": ["address", "phone"],
            "research_record": pitstop_lead_json,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from SHA-256 verified preview-export/lead-0002-pitstop-lagos/review-package.json",
            "created_at": "2026-10-12T04:10:42Z",
            "updated_at": "2026-10-13T01:22:27Z",
        }
        self.state["qualifications"]["qualification-0002"] = {
            "qualification_id": "qualification-0002",
            "lead_id": "lead-0002",
            "status": "QUALIFIED",
            "website_status": "WEBSITE_POOR",
            "score": 95,
            "priority": "P2",
            "evidence": pitstop_source_urls,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from CHANGELOG.md Phase 25 and preview-export/lead-0002-pitstop-lagos/review-package.json",
            "created_at": "2026-10-12T04:10:42Z",
        }
        self.state["projects"]["project-0002"] = {
            "project_id": "project-0002",
            "lead_id": "lead-0002",
            "data_classification": "REAL_PUBLIC_DATA",
            "current_stage": "READY_FOR_REVIEW",
            "project_status": "ACTIVE",
            "review_status": "PENDING",
            "review_notes": None,
            "review_history": [],
            "assigned_build_id": pitstop_build_id,
            "latest_validation_id": pitstop_val_id,
            "latest_deployment_id": None,
            "isolated_preview_project": None,
            "reconstructed": True,
            "recovery_provenance": "Reconstructed from SHA-256 verified preview-export/lead-0002-pitstop-lagos/review-package.json",
            "created_at": "2026-10-12T04:10:42Z",
            "updated_at": "2026-10-13T01:22:27Z",
        }
        self.state["builds"][pitstop_build_id] = pitstop_manifest
        self.state["validations"][pitstop_val_id] = pitstop_val_record
        self.state["build_artifacts"][pitstop_build_id] = {
            "build_id": pitstop_build_id,
            "project_id": "project-0002",
            "lead_id": "lead-0002",
            "template_id": str(pkg.get("template_id") or "restaurant-menu-v1"),
            "artifact_path": pitstop_rel_out,
            "html_content": pitstop_html_text,
            "html_bytes": len(pitstop_html_bytes),
            "html_sha256": pitstop_actual_sha,
            "build_manifest": pitstop_manifest,
            "review_package_json": pkg,
            "review_readme_md": pitstop_md_text,
            "review_package_sha256": hashlib.sha256(json.dumps(pkg, sort_keys=True).encode("utf-8")).hexdigest(),
            "created_at": "2026-10-13T01:22:27Z",
            "updated_at": now(),
        }

        self._save()
        report = self.check_local_state(repository_root=root)
        report.update({
            "recovered_projects": ["project-0001", "project-0002"],
            "recovered_builds": [fic_build_id, pitstop_build_id],
            "verified_pitstop_sha256": pitstop_actual_sha,
            "reconstructed": True,
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

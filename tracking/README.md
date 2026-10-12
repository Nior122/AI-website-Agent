# Lead & Project Tracking

`TrackingStore` is the single local JSON state store (`data/tracking/state.json`, ignored by Git). It preserves leads separately from projects and preserves build, validation, and deployment history by ID. No database or external service is required.

Create a lead, then a project only for `QUALIFIED` leads. Use guarded transitions; record builds, validations, deployments, and human review without overwriting history. `snapshot(project_id)` returns the current lead, project, latest records, and derived next action.

Duplicates use public identifiers: exact website domain, phone, or social URL gives `EXACT_MATCH`; same normalized name plus city/country gives `POSSIBLE_MATCH`; otherwise `NO_MATCH`. Possible matches are retained for review, not merged.

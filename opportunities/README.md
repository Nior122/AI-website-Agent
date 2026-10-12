# Website Opportunity Qualification

`assess_opportunity(candidate)` converts one source-backed Scout candidate into a transparent qualification record and a Website Content Brief. It separates `verified`, `unknown`, and `suggested` content. It never creates a project or invokes the Builder.

A missing website field on a directory/listing is **not** proof that the business has no website. It is returned as `WEBSITE_UNKNOWN`, with `NEEDS_REVIEW`, until additional evidence establishes ownership or absence. Only a `QUALIFIED` record with sufficient evidence can be used to create a tracked project; `TrackingStore.create_project()` is idempotent per active lead.

For real public data, keep the result review-only and local until a human explicitly approves the next stage. No adapter sends contact or outreach traffic.

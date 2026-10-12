# Website Validation & Quality Assurance

Phase 6 validates a local generated HTML artifact before any future deployment stage. It is dependency-free, repeatable, and static: it neither opens a browser nor makes network requests.

## Run validation

```powershell
python -m validation.cli --html output/<lead-id>/<build-id>/index.html --business-name "Fictional Glow Studio" --real-build
```

The command prints a structured result only. It never deploys, contacts a business, follows external links, or modifies the artifact.

## Result and states

Validation states are `PENDING`, `RUNNING`, `PASSED`, `FAILED`, and `NEEDS_REVIEW` at orchestration level. The current result uses:

- `PASS`: no findings of any severity.
- `FAIL`: at least one `CRITICAL` or `ERROR` finding.
- `NEEDS_REVIEW`: warnings only.

The score starts at 100 and deducts 100 for critical, 20 for error, and 5 for warning. It is descriptive only: a critical/error always fails regardless of score.

## Checks

- build/content integrity: parseable output, landmarks, H1, and business name;
- links/contacts: internal anchors, URLs, `tel:`, `wa.me`, and expected contact CTA presence;
- images/accessibility: absolute image URLs and alt text;
- responsiveness: viewport metadata and responsive CSS presence;
- SEO: title and factual meta description;
- security: secret-like patterns, redacted in reporting;
- placeholder/demo content and suspicious business claims;
- performance: large HTML artifact warning.

`repair_suggestion` may be supplied with a finding, but this validator never edits code or HTML.

For a clearly labeled fictional fixture only, callers may set `synthetic_contact=True`. This suppresses missing phone/WhatsApp CTA warnings when the renderer intentionally omits synthetic live-contact destinations. It must not be used for real-business records.

## Limitations

It cannot determine if an external URL actually resolves, measure color contrast, inspect computed mobile layout/overflow, or prove a claim is false. Such checks require an approved browser/runtime capability and source-backed business research; until then suspicious claims are warnings for human review.

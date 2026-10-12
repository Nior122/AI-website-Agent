# Website Builder

The Website Builder converts one `QUALIFIED` research record into a local static preview artifact. It uses only verified data carried in that record and never deploys, contacts a business, or calls an AI provider by default.

## Build flow

```text
Qualified research record → verified-data mapping → template selection
  → content generator → local HTML render → validation → output/<lead-id>/<build-id>/
```

The result includes build ID, lead ID, template ID, state, generated content, validation, output path, and errors. A failed build leaves the input lead unchanged. A later orchestrator can retry the same record or preserve prior output directories.

## Content generation

`ContentGenerator` is a provider-neutral interface. The included `DeterministicContentGenerator` is the default: it only rearranges verified business name, city, description, contacts, and service names. It does not make claims, generate testimonials, or require a model.

A future adapter may call an approved provider, but must return the same structured content and pass `_assert_content_grounded`. The builder rejects a hero title other than the verified business name and rejects body content that does not contain a supplied fact. Credentials belong outside this repository and are not required now.

## Facts, brand, and assets

- A record must have `research.qualificationStatus: QUALIFIED`.
- Template data is mapped from `research.verifiedFacts`; the candidate is used only as a compatible record container.
- Brand values apply only from `approved_brand.values` when `approved_brand.sourceUrls` is non-empty. Otherwise template defaults are used.
- Images apply only from `approved_assets` entries marked `approved: true`, with a source URL, image URL, and alt text. Missing assets simply omit the gallery.
- Testimonials, reviews, awards, prices, booking systems, and payment flows are not generated.
- `TEST_FIXTURE`/`demo` data stays visibly fictional and uses non-actionable in-page CTAs. The builder passes `synthetic_contact=True` only for that mode, so fixture contacts do not become misleading live destinations; real records remain subject to normal contact-CTA validation.

## Build states

`PENDING → ANALYZING → GENERATING_CONTENT → BUILDING → VALIDATING → COMPLETED`

Failures become `FAILED`; invalid output becomes `NEEDS_REVIEW`. The current synchronous implementation returns the final state, while retaining the full state vocabulary for future orchestration.

## Validation

Phase 6 routes every build through the independent `validation/` QA gate. It checks integrity, content, links, images, contacts, static responsiveness, accessibility landmarks, SEO basics, potential secrets, placeholders, suspicious claims, and basic artifact size. Only `PASS` writes an artifact; `FAIL` and `NEEDS_REVIEW` are not deployment-ready. See `validation/README.md`.

## Local example

```powershell
python -m builder.cli --lead tests/fixtures/qualified_lead_salon.json
```

This writes a fictional preview below `output/fictional-salon-001/<build-id>/index.html`. `output/` is ignored by Git and is never deployed automatically.

## Local preview inspection

To inspect a generated preview locally without external dependencies:

1. **Direct file view**: Open `output/<lead-id>/<build-id>/index.html` directly in any browser (`file:///...`).
2. **Localhost static server**: Run the standard-library static server bound strictly to localhost:
```powershell
python -m builder.cli --serve output/<lead-id>/<build-id> --port 8080
```
This serves `http://127.0.0.1:8080/` strictly from the designated artifact folder and does not expose it to the network.

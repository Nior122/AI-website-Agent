# Research & Qualification Agent

Phase 3 turns one Scout candidate into an evidence-led qualification record. It is fixture-only: `FixtureResearchSource` reads fictional local evidence and cannot browse, scrape, or contact anyone.

## Pipeline

```text
Candidate → source-backed evidence → identity check → facts / uncertainties
          → website evaluation → activity evaluation → QUALIFIED / REVIEW / SKIP
```

The candidate remains unchanged under `candidate`. The `research` object is additive and records all conclusions separately.

## Safe run

Use only the fictional fixture and a candidate JSON object. No network call is made.

```powershell
python -m research.cli --dry-run --candidate-file tests/fixtures/research_candidate.json --evidence-file tests/fixtures/research_evidence.json
```

## Evidence and identity

Every `verifiedFacts` item carries a field, value, and source URL. Inferences state their basis; unknown or unverified items go in `uncertainties`.

An evidence item is identity-verified only when at least two independent signals agree: name, city/country, phone, or Instagram URL. One signal is `review`; none is `mismatch`. Mismatched evidence is never merged into a candidate.

## Website classifications

`research.websiteEvaluation.status` is the authoritative Phase 3 classification; Scout's `websiteStatus` remains preliminary.

| Status | Criteria |
| --- | --- |
| `NO_WEBSITE` | No website appears in reviewed candidate evidence. |
| `WEBSITE_UNAVAILABLE` | An approved observation records blocked, unavailable, or timeout. |
| `WEBSITE_BASIC` | It loads for the correct business but evidence does not establish modern core usability. |
| `WEBSITE_OUTDATED` | A source-backed observation identifies outdated information. |
| `WEBSITE_NEEDS_IMPROVEMENT` | It loads but an observed usability/content/trust issue is recorded. |
| `WEBSITE_MODERN` | It loads for the correct business and mobile, navigation, services, contact, and CTA are all positively observed. |
| `UNKNOWN` | The site, business identity, or inspection is not adequately verified. |

These are evidence-based classifications, not design-fashion judgments. A later approved browser/verifier may populate observations; until then website status stays `UNKNOWN` rather than inferred.

## Qualification

`QUALIFIED` requires verified identity, active public signals, sufficient source-backed information, and an opportunity status (`NO_WEBSITE`, unavailable, outdated, or needs improvement). `REVIEW` handles uncertainty, source errors, limited information, or uncertain website quality. `SKIP` applies to mismatched evidence or a verified modern website. Reasons are specific to the record.

No numeric score is used. `opportunityFactors` holds the transparent inputs: legitimacy, activity, information sufficiency, website status, social presence, and preview readiness.

## Future integration

Future live research sources must implement `ResearchSource.research(candidate)`, be explicitly approved, comply with terms/law, record source URLs, and return public evidence only. Do not add a browser, API, account, or provider integration without approval.

## Targeted real-data re-verification

`research.reverification.build_reverification_event` is an offline audit-record
helper, not a browser or scraper. It is for an explicitly approved, bounded
public-data check. The caller supplies source-backed evidence and the helper
preserves the previous evidence URLs, source classifications, result, and the
fact that no lead or project was created by the research step.

It cannot infer `NO_WEBSITE` from an absent listing field. A property must be
classified explicitly (`OFFICIAL`, `LIKELY_OFFICIAL`, `DIRECTORY`, `SOCIAL`,
and so on), with unresolved ownership retained as an uncertainty.

The Phase 15 Cactus record is deliberately kept under ignored `data/leads/` as
`REAL_PUBLIC_DATA`; it is not a fixture, project, deployment, or build input.

# Research & Qualification Agent

Phase 3 turns one Scout candidate into an evidence-led qualification record. The default source, `FixtureResearchSource`, reads fictional local evidence and cannot browse, scrape, or contact anyone. A second, explicitly permitted source - `PublicWebsiteResearchSource` - reads the public web pages the candidate record *already names*, under the bounds below, and nothing else.

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

## Bounded live website research

```powershell
python -m research.cli --dry-run --source live --candidate-file data/leads/candidate.json --output data/leads/research-live.json
```

Two keys must both be turned by the operator before any request leaves the machine: `AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH=1` in the environment, and `research.allowLiveResearch: true` in `config/system.settings.local.json` (copied from `config/system.settings.example.json`). Missing either, the command refuses with exit code 2 and no request is constructed; a job input or workflow cannot substitute for either, which is why the n8n pipeline does not carry `live_research` at all.

What the live source is allowed to do, and what the code refuses:

| Rule | Enforcement |
| --- | --- |
| Only URLs already on the record | `targets_for()` reads `originalWebsite` (and social profiles only if allowlisted); no search, no crawl, no sub-page discovery |
| Public pages only | `permitted_public_target()` refuses credentials, non-standard ports, non-http schemes, single-label names, `.local`/`.internal`/`.invalid`/`.test`, private/reserved IP literals, and names that resolve privately |
| Source terms and access rules | `robots.txt` is fetched first (counting against the budget) and a disallowal stops the page request; `Disallow` is recorded as `ROBOTS_DISALLOWED`, never negotiated |
| No login, CAPTCHA, or bypass | 401/403/`Retry-After`-limited 429 are recorded as `ACCESS_RESTRICTED`/`RATE_LIMITED`; a login page is `LOGIN_WALL`; platform hosts (Instagram, Facebook, X, LinkedIn, TikTok) are not fetched unless explicitly allowlisted *and* `fetchPlatformProfiles` is true |
| Bounded traffic | `maxPagesPerCandidate` (1-3), `maxRequestsPerRun` (1-12), `maxResponseBytes`, `requestTimeoutSeconds`, `maxRedirects`, `maxRetries` (0-2), `minDelaySeconds`; every bound can be lowered by configuration, never removed |
| Traceability | Each finding carries `source_url`, `observed_at`, a short `excerpt` of literal page properties, and a `retrieval` block (`status`, `http_status`, `attempts`, `requests_used`, `robots`, `bytes`, `content_type`, `uncertainty`) |
| No invented findings | Flags are set only from countable page properties (viewport meta, nav/anchor counts, `tel:`/`mailto:` presence, a copyright year); `untrustworthy` and `broken_links` are never asserted from one page |
| No false negatives | A page that could not be read produces no `website_observation`, so `_evaluate_website` answers `UNKNOWN`; `NO_WEBSITE` still requires the absence to be in the reviewed candidate evidence |
| No action | Research imports nothing from the builder, reviewer, deployer, or outreach modules and writes no state; `QUALIFY`, `BUILD`, review, and deployment stay separate jobs with their own gates |

If no permitted page could be read, the source raises rather than returning empty: the job records `LIVE_RESEARCH_SOURCE_UNAVAILABLE` (or `LIVE_RESEARCH_NOT_ENABLED` when the deployment has not permitted live research at all) with the per-attempt status retained, and `research/README.md`'s classifications stay untouched.

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

# Scout Agent

The Scout Agent is a small, offline-first Phase 2 component. It discovers candidate records through replaceable source adapters, normalizes and deduplicates them, then produces explainable initial qualifications. It does not build websites, browse the web, scrape, deploy, or contact businesses.

## Safe first run

The included fixture contains explicitly fictional businesses and `example.invalid` URLs. It makes no network requests.

```powershell
python -m scout.cli --dry-run --source-file tests/fixtures/scout_candidates.json --country Nigeria --city "Port Harcourt" --niche "Beauty salons" --lead-limit 3
```

The command writes the job result to standard output only. To save an inspected result locally, supply `--output data/leads/scout-dry-run.json`; that directory is ignored by Git.

### Bounded live discovery, only when asked

```powershell
python -m scout.cli --dry-run --source overpass --approve-live-lookups --lead-limit 3 --output data/leads/scout-live-review.json
```

That flag is what permits one read-only Overpass request; without it the command refuses with exit code 2 before the source object exists, so an unapproved run cannot accidentally go out. `--max-requests` is accepted only between 1 and 5 and `out center` stays capped, which keeps a run polite to a shared public API. Optional `--endpoint` / `--fallback-endpoint` exist for the documented timeout-then-fallback behaviour. The live-run notice is written to standard error so the JSON document on stdout (or in `--output`) stays parseable. A source that fails yields status `failed` and exit code 1 - an empty funnel and a broken one are never reported the same way.

Run tests with:

```powershell
python -m unittest discover -s tests -v
```

## Component contract

`Scout(Target, sources).run()` accepts a target (`country`, `city`, `niche`, `lead_limit`, and `dry_run`) and returns a `ScoutJobResult` with:

- `status`: `pending`, `running`, `completed`, `partial`, or `failed`;
- normalized, deduplicated `leads`;
- source and candidate counters; and
- recoverable errors without discarding successful source results.

`FixtureSource` is always safe for tests. `OverpassRestaurantSource` is the sole approved live pilot: one read-only OpenStreetMap Overpass request for Lagos, Nigeria restaurants - a union of three documented tag clauses (Facebook handle, Instagram handle, all restaurants) over a bounded box, `out center` capped at twenty - from which at most five named candidates survive, chosen by priority rather than by arrival order, and no endpoint is retried beyond the request budget the run declared. It preserves each public OSM element URL and marks normalized records `REAL_PUBLIC_DATA`. It does not visit the business website, social profiles, or contact links, and it does not create a tracked lead/project or contact anyone.

The live pilot is review-only. A missing `website` tag in OpenStreetMap means only `none_known` **in that source**—not proof that the business has no website. Any candidate needs evidence-led Research before becoming a usable lead. Local real-data review exports belong under ignored `data/leads/`; never put real data in test fixtures or commit it unintentionally.

## Qualification model

Qualification is a transparent initial triage, not a claim that a website is bad. A record must have a name, target-city/country match, at least one source URL, and an explicit active signal (social profile, phone, WhatsApp, opening hours, services, or description).

Website categories drive the result:

| Website status | Qualification result | Opportunity score |
| --- | --- | --- |
| `none_known` | `qualified` | 4 |
| `poor` / `outdated` | `qualified` | 3 |
| `basic` / `unknown` | `lower_priority` / `needs_review` | 1 |
| `professional` | `skipped` | 0 |

`onlinePresenceScore` is an evidence-coverage count from 0–5, not a popularity or quality score: one point each for a social URL, contact channel, services, opening hours, and description. The qualification reasons include the concrete evidence used. Only an approved future verifier may label a website `poor`, `outdated`, `basic`, or `professional`; otherwise use `unknown`.

## Deduplication and provenance

Names are normalized by lowercasing and removing punctuation/spacing. Candidates merge when their normalized business name and normalized city/country are identical. Exact shared phone, website host, Instagram/Facebook URL, or WhatsApp also permits a merge for records with the same normalized name. The agent does not use fuzzy-name matching, so ambiguous records remain separate for review.

Merged leads preserve the union of `sourceUrls` and `discoverySources`; unknown information remains `null`. `fieldSources` identifies which source URLs contributed each known field, so later agents can answer where a value came from. `lastCheckedAt` records the local job's check time; `discoveredAt` remains `null` until an approved source supplies a verifiable discovery time.

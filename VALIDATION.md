# Phase 0 validation results

- **Run environment:** local / residential IP
- **Started (UTC):** 2026-08-17T14:42:58+00:00
- **Duration:** 4.1 min
- **HTTP:** 61 req, 403=0 429=0 5xx=0 empty200=0 err=0 (0.0% anomalous)
- **Status codes:** {200: 29}

| # | Test | Gate | Result | Detail |
|---|------|------|--------|--------|
| T1 | Play realInstalls exact | yes | PASS | 4 exact / 0 banded / 0 failed of 4 |
| T2 | Play sustained volume | yes | PASS | 15 fetches: 15 ok, 0 parser-null (0.0%), 0 anomalies (0.0%) |
| T3 | Apple endpoint surface | yes | PASS | chart cap 100; new-app feeds working: newapplications, newfreeapplications, newpaidapplications; lookup 200/200; reviews xml=50 json=50; utc parse ok |
| T4 | Young-app review depth | info | PASS | 4 apps aged 30-90d: 1 truncated at the 500 cap (25%). Backfill is effectively complete |
| T5 | Play reviews paging | info | PASS | 2 pages, 200 reviews, oldest 2026-08-16 |
| T6 | Search sweep + band filter | info | PASS | 74 distinct apps / 3 terms (cap 30/term); young rate low-band 0% vs high-band 0% — band pre-filter NO BETTER than random |
| T7 | Developer-page discovery | info | PASS | 2/2 developer pages parsed via `developer?id=<name>` (legacy `dev?id=` is 404). Regex parser, no library support |

## Verdict

**All gates passed.** Proceed to Phase 1 (schema + Play pipeline).

## Residential baseline (2026-08-12) for comparison

| Fact | Baseline |
|---|---|
| Play `realInstalls` | exact — Spotify 3,084,933,551 vs min 1,000,000,000 |
| Apple chart cap | 100 per genre (`limit=200` still returns 100) |
| Apple `newapplications` | 100 entries per genre |
| Apple reviews XML | 50 entries per page, 10 pages max |
| Apple reviews JSON | 50 entries (the zero-entry bug did not reproduce) |
| Play `NEW_FREE` cluster | dead — HTTP 200, zero app links |
| Play sitemaps | no `<lastmod>`, mixed content types — useless for new-app discovery |
| Play `developer?id=<name>` | 200 + parseable app links (`dev?id=` is 404) |
| `play_search` on thin results | raises TypeError (search.py:41) — must be wrapped |

See FINDINGS.md for the full evidence log and the spec deltas each finding forces.

# Issue reports and submission preparation

Maintain all issue URLs, submission states, evidence links and current Pattern mappings in the root [bugs.csv](../bugs.csv). It is the single maintained registry. The root README tables are generated from that file and include only published `pending`, `confirmed` and `fixed` issues.

Unsubmitted rows use an empty `issue_number` and `issue_url`. Their `status_evidence` points to the local report:

- `draft`: an English report and native reproducer are prepared, with locally verified results, timings and profiles. It has not been submitted.
- `needs_review`: a preserved historical report has no matched submitted row; its tested version, source ownership or duplicate relationship still needs review.
- `follow_up`: current evidence should supplement a related public issue; no comment has been posted, and an exact duplicate root cause is not assumed.
- `merged`: a local dossier supports another local report; `duplicate_of` points to that representative.
- `archived`: retained evidence does not warrant a new provider issue under the recorded decision; this does not claim that the observed behavior was fixed.

Use `pending` only after an upstream issue has actually been created. Fill its issue number and URL at that point. Preserve declined or merged published reports in the CSV; their status controls whether they appear in the root README.

The [2026-10-31 preparation review](CANDIDATE_REVIEW_20261031.md) is a dated audit of saved candidate records and historical reports, not another live status table. Its [per-record evidence](evidence/candidate-audit-20261031.json) distinguishes discoveries, replays, copied inputs, native confirmations and the intentional executor positive control. The [legacy provenance index](evidence/legacy-provenance-20261031.json) resolves evidence paths from the pre-refactor checkout.

Directory names such as `P3`, `P4` or `S7` in older reports use earlier catalogs. Current mappings and their reasons belong in `bugs.csv`. Reports are not counted once per seed, scale, replay or related Pattern.

New or revised reports follow [AGENTS.md](AGENTS.md): four English sections, complete setup, exact queries and results, ordinary timing separated from native profiles, and local provenance. The [Neo4j path report](Neo4j/P3/BUG001.md) and [Boolean report](Neo4j/P4/BUG002.md) remain writing examples; their measurements belong to their recorded experiments.

The [2026-10-31 historical verification](LEGACY_REVIEW_20261031.md) resolves the 13 historical dossiers into six new report proposals, three follow-ups, one merge and three archives. The registry contains no remaining `needs_review` rows from that set. Current-version measurements and source/upstream comparisons are separate from the preserved historical report bodies.

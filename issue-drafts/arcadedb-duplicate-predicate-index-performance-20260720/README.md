# Evidence boundary

This directory contains a current-version candidate that passed the runtime
relative gate in two independent sessions.

- Primary oracle: ordinary `execute` runtime only.
- Complete result equality: passed in both sessions and every timing pair.
- Fresh state: two separate databases, client processes, connections, schema
  builds, unique indexes, and graph loads.
- Protocol: 5 warmups and 20 balanced A/B pairs per session, plus A/A and B/B
  negative controls.
- Pooled result: 13.19x ratio, ratio CI low 10.83x, 32.57 ms delta, direction
  consistency 1.0.
- Rule boundary: `duplicate_predicate_control` is tier `control` and has
  `submit_eligible=false`; the evidence therefore received manual review and
  deduplication instead of automatic promotion.
- `EXPLAIN` is explanatory evidence only and was not used as the runtime oracle.

Files:

- `ISSUE.md`: submission-ready issue body.
- `DEDUP.md`: comparison with #4405, #5107, #5116, and #5117.
- `verify.py`: certificate-bound independent-session verifier.
- `session1.json`, `session2.json`: raw paired timing and negative controls.
- `validation_summary.json`: pooled validation statistics.
- `scaling.json`: explanatory 5k/20k/80k scaling check.
- `setup.sql`, `setup.cypher`, `query_a.cypher`, `query_b.cypher`: compact reproducer.
- `plan_a.txt`, `plan_b.txt`: explanatory plan excerpts.
- `environment.json`: checked image and host identity.

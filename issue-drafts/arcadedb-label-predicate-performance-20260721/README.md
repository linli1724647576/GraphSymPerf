# Evidence boundary

This directory contains a current-version ArcadeDB performance candidate that
passed independent runtime validation in two fresh databases.

- Primary oracle: ordinary query execution runtime.
- Complete ordered result equality: passed for every timing pair.
- Fresh state: separate databases, client processes, schemas, type hierarchies,
  and 300,000-row unrelated data loads.
- Protocol: 5 warmups, 20 balanced A/B pairs, and separate A/A + B/B controls
  per session.
- Pooled result: 61.17x, ratio CI low 46.55x, 153.37 ms median delta,
  direction consistency 1.0.
- Pooled negative-control p99: 56.59 ms, below the A/B median delta.
- Rule boundary: `label_where_symmetry` is experimental and
  `submit_eligible=false`; manual semantic review and independent validation
  were required.
- `EXPLAIN` and the scale check are explanatory, not runtime oracles.

Files:

- `ISSUE.md`: submission-ready issue body.
- `DEDUP.md`: upstream and local deduplication decision.
- `verify.py`: fresh-database verifier.
- `session1.json`, `session2.json`: raw timings, controls, results, plans, and
  framework certificate.
- `validation_summary.json`: pooled runtime statistics.
- `scaling.json`: explanatory scaling check.
- `setup.sql`, `setup.cypher`, `query_a.cypher`, `query_b.cypher`: reproducer.
- `plan_a.txt`, `plan_b.txt`: checked plan text.
- `environment.json`: checked image and host identity.

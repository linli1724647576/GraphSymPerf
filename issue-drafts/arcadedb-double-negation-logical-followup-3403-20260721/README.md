# Evidence boundary

- Correctness oracle: complete ordered result rows.
- Independent validation: two fresh databases and separate client processes.
- Repetition: every query and control ran five times per session with
  deterministic results.
- Three-valued coverage: `true`, `false`, and missing/`null` Boolean property.
- Framework boundary: `double_negation_predicate` is experimental and
  `submit_eligible=false`; the transform was manually reviewed.
- Dedup boundary: related to #3403, so the recommended artifact is a follow-up
  comment first, not a new issue.
- Plans are explanatory only; the result mismatch is sufficient.

Files:

- `FOLLOWUP_3403.md`: recommended first action.
- `ISSUE.md`: issue body if maintainers request separate tracking.
- `DEDUP.md`: comparison with #3403 and its fix commit.
- `reproducer.py`: fresh-database independent verifier.
- `session1.json`, `session2.json`: complete raw results, controls, plans, and
  framework certificate.
- `setup.sql`, `setup.cypher`, `query_a.cypher`, `query_b.cypher`: compact
  reproducer.
- `environment.json`: checked current image identity.

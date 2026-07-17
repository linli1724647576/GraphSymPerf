# Draft status: rejected by the current submission policy

This directory contains a GitHub-issue-style draft requested for historical
candidate `f0a4e119b9b4cdef2a12d7d94fcfc988f2ef55d1c28ed565c220b661b257bc26`.

The independent 12-pair pre-screen confirmed equivalent complete results and a
positive multi-second absolute delta. However, it failed two mandatory
predicates of the configured strong-absolute submission path:

- median ratio `1.18768x` is below `1.5x`;
- ratio confidence-interval lower bound `1.12455x` is below `1.2x`.

Accordingly, this draft must not be counted as `submit_eligible`, a validation
pass, or `validated_runtime_yield`. It is supplied as a copy-editable report of
the observation because the user explicitly requested an issue-format draft.

The transformation also matches Neo4j #13813 and should be treated as likely
supplemental evidence for that existing issue family, not a distinct discovery.

Files:

- `ISSUE.md`: copy-editable developer report in the format of prior reports.
- `setup.cypher`: complete 1,280-node graph attachment.
- `query_a.cypher` and `query_b.cypher`: the independently screened query pair.
- `evidence_summary.json`: historical and independent evidence plus rejection.

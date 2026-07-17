# Draft status: independently validated, but not submit-ready

This directory contains a GitHub-issue-style draft requested for candidate
`f9891d29092edfaa5bd39b26cf3552f32194026c4b6ed919f74b08c5a3dec994`.

The original full query passed strict independent validation in two fresh
sessions with 20 pairs per session: pooled ratio `24.82700x`, pooled delta
`32.080 ms`, ratio confidence-interval lower bound `22.31071x`, and delta
confidence-interval lower bound `31.295 ms`.

However, the live reducer accepted none of 24 safe deletion proposals. Its
final five-pair recheck of the unchanged query still measured `20.30631x`, but
the absolute delta (`17.836 ms`) was below the sealed noise floor
(`26.384 ms`). The reducer therefore returned `reduction_invalidated` with
`final_runtime_failed`; reduced-case validation was not reached.

In addition, the source v6 run was provenance-invalidated after files were
created in the repository while the run was active. This validation artifact
must not be counted in formal v6 `validated_runtime_yield`.

The transformation also matches the already reported Neo4j issue #13813, so
the default disposition is a non-submittable follow-up reproducer unless a
fresh clean-manifest run produces a reduced, independently revalidated case and
deduplication establishes a distinct root-cause family.

Files:

- `ISSUE.md`: copy-editable developer report in the format of prior reports.
- `setup.cypher`: complete 1,280-node graph attachment.
- `query_a.cypher` and `query_b.cypher`: the measured query pair.
- `evidence_summary.json`: source-bound validation, reduction, provenance, and
  deduplication blockers.

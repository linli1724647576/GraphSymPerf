# Neo4j #13813 regression follow-up evidence (v4 / 0aa2)

## Classification

This package is **follow-up regression evidence for neo4j/neo4j#13813**, not a new issue package.

- Do not count it as a new discovery or in `validated_runtime_yield`.
- Do not submit it as a separate issue unless minimization, reduced-case validation, and duplicate review establish a distinct root-cause family.
- The safe immediate action is to paste `FOLLOWUP_COMMENT.md` into the existing issue.

## Verified result

- DBMS: Neo4j Community `2026.05.0`
- Rule: `pattern_order_symmetry` (`CORE`, `submit_eligible=true`)
- Graph: 1,280 nodes and 2,237 relationships
- Pair hash: `0aa2c010e88cff0e00e93f6216a593358ebdfc46fb6c1bc24914b93c2e907b99`
- Difference: only the order of two comma-separated pattern components in one `MATCH` clause
- Independent validation: two fresh process sessions, 20 balanced query pairs per session
- Pooled validated ratio: `14.504939x`
- Ratio confidence-interval lower bound: `14.361499x`
- Pooled median delta: `586.099334 ms`
- Delta confidence-interval lower bound: `573.710667 ms`
- Direction consistency: `1.0` in both sessions
- Negative controls: passed in both sessions
- Result equivalence: complete results matched

## Remaining evidence gap

The v4 pipeline failed after independent validation because the then-current Task-8 sealed-validation reference was not bound to the candidate evidence. That code defect was fixed later in commit `018d76c`, but this historical run has no reducer output, reduced-case revalidation, dedup clearance, or formal `SubmitReadyIssue` artifact.

The full 1,280-node graph snapshot is preserved inside the original validation artifact. It should be extracted only as an attachment if the maintainer requests it; a minimized reproducer is still preferable.

## Source artifacts

- Candidate: `runs/neo4j-pattern-order-1280-10x1000-20260717-v4/entries/p0-neo4j-discovery-pattern_order_symmetry-1280-0-b001d9dc7e0c/candidates/0aa2c010e88cff0e00e93f6216a593358ebdfc46fb6c1bc24914b93c2e907b99/candidate.json`
- Independent validation: `runs/neo4j-pattern-order-1280-10x1000-20260717-v4/entries/p0-neo4j-discovery-pattern_order_symmetry-1280-0-b001d9dc7e0c/validation/0aa2c010e88cff0e00e93f6216a593358ebdfc46fb6c1bc24914b93c2e907b99.json`
- Existing issue: https://github.com/neo4j/neo4j/issues/13813

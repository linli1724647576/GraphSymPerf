# Deduplication note

Checked on 2026-07-22 against the complete LadybugDB issue list and targeted
GitHub searches in both `LadybugDB/ladybug` and the archived `kuzudb/kuzu`
repository.

Search terms included:

- `DISTINCT primary key`
- `distinct performance aggregate optimizer`
- `group primary key optimizer`
- `count(DISTINCT)`

No existing report has the same trigger and performance symptom. The nearest
reports have different scopes:

- [LadybugDB #529](https://github.com/LadybugDB/ladybug/issues/529) rewrites
  relationship-degree queries such as `count(DISTINCT node.pk)` and tracks a
  direction-dependent planner case. This candidate is a plain node scan and
  asks whether a redundant `DISTINCT` on an already unique projection can be
  removed.
- [Kùzu #5250](https://github.com/kuzudb/kuzu/issues/5250) reported a crash in
  `count(DISTINCT b)` on Kùzu 0.9.0. It was fixed and does not report a
  primary-key performance regression.
- [Kùzu PR #1517](https://github.com/kuzudb/kuzu/pull/1517) avoids hashing
  projected properties that functionally depend on a grouped primary key. It
  still keeps the primary key itself as a grouping key, whereas this candidate
  concerns eliminating the whole redundant distinct operation.
- [Kùzu #5228](https://github.com/kuzudb/kuzu/issues/5228) proposes removing a
  `WITH` boundary between two pattern matches. This candidate has one match and
  is triggered specifically by `DISTINCT` on a primary key.

Conclusion: not a duplicate based on trigger, symptom, or expected fix scope.

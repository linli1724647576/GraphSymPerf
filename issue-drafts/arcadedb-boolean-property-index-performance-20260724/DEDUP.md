# Deduplication decision

Decision: **new issue is justified**.

Checked against the current ArcadeDB issue tracker on 2026-07-24.

| Existing issue | Existing trigger and fix | Current trigger | Decision |
|---|---|---|---|
| [#5360](https://github.com/ArcadeData/arcadedb/issues/5360) | A bare `NOT NOT n.flag` parser bug changed the selected rows. | There is no `NOT`; both direct and explicit-`true` forms return the same rows. The difference is index selection. | Same example property, different symptom and component. |
| [#5362](https://github.com/ArcadeData/arcadedb/issues/5362) | Indexed equality inside `AND` was not extracted. | The direct Boolean predicate contains no equality for the existing `AND` walker to extract. | Related index symptom, different planner rule. |
| [#4405](https://github.com/ArcadeData/arcadedb/issues/4405) | Constant folding for a larger Boolean expression. | Direct Boolean-property truth testing. | Not duplicate. |

Searches included `boolean index`, `WHERE n.flag`, `flag = true`,
`NodeIndexSeek`, `NodeByLabelScan`, and `Boolean property predicate`. No issue
with the same trigger was found.

This draft does not overlap the De Morgan or `CASE` drafts: it requires the
planner to recognize an indexed Boolean property as an implicit `= true`
condition, without rewriting `NOT`, `OR`, or `CASE`.

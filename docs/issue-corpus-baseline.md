# 已公开 issue corpus 基线

本表保存用户在 2026-07-10 提供的 11 条公开 issue URL 与状态快照。它们是
`Reported Issue Report`，不自动等同于 `Developer-Validated Bug`，也不表示已由
runtime-first v3 validator 复核。状态与证据缺失均原样记录，不补造维护者结论。

| DBMS | URL | 用户提供的状态 | 当前提交中的本地证据 | 缺失或待核查项 |
| --- | --- | --- | --- | --- |
| Neo4j | https://github.com/neo4j/neo4j/issues/13783 | fixed | `BUG list/BUG001_replay_summary.json`、`BUG list/reproducer_graph.cypher.txt`（关联关系待 Task 10 核查） | 完整 query pair、reduced pair、独立 validation、版本归因 |
| Neo4j | https://github.com/neo4j/neo4j/issues/13814 | uninterested | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| Memgraph | https://github.com/memgraph/memgraph/issues/4208 | confirmed | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| FalkorDB | https://github.com/FalkorDB/FalkorDB/issues/2081 | waiting reply | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/4405 | fixed | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| Apache AGE | https://github.com/apache/age/issues/2428 | uninterested | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| LadybugDB | https://github.com/LadybugDB/ladybug/issues/668 | waiting reply | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| LadybugDB | https://github.com/LadybugDB/ladybug/issues/669 | waiting reply | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| Neo4j | https://github.com/neo4j/neo4j/issues/13813 | confirmed | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/5116 | fixed | 未找到可唯一归属的本地文件 | 全部最小证据字段 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/5117 | fixed | 未找到可唯一归属的本地文件 | 全部最小证据字段 |

状态计数为 4 fixed、2 confirmed、2 uninterested、3 waiting reply。`waiting reply`
是观察期内的右删失状态，不是负例。当前提交仅保留 3 个 `BUG list/` 文件，且其中
`BUG list/README.md` 引用了已不存在的相对路径；Task 10 必须建立逐 issue 的
evidence package 和缺失原因，不能从当前残留文件推导 runtime 验证成功率。

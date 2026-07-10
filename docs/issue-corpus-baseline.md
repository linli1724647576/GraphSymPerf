# 已公开 issue corpus 基线

本表仅保存用户在 2026-07-10 提供的公开 issue URL 与状态快照。它们是独立的
`Reported Issue Report`，不等同于 `Developer-Validated Bug`；更不代表已经由后续 v3
validator 重新验证。

| DBMS | URL | 用户提供的开发者状态 | 当前本地证据位置 | 说明 |
| --- | --- | --- | --- | --- |
| Neo4j | https://github.com/neo4j/neo4j/issues/13783 | fixed | `BUG list/neo4j/0_SYM_BUG001/` | 待迁移最小证据包 |
| Neo4j | https://github.com/neo4j/neo4j/issues/13814 | uninterested | `BUG list/neo4j/` | 具体目录待 Task 10 盘点 |
| Memgraph | https://github.com/memgraph/memgraph/issues/4208 | confirmed | `BUG list/memgraph/` | 具体目录待 Task 10 盘点 |
| FalkorDB | https://github.com/FalkorDB/FalkorDB/issues/2081 | waiting reply | `BUG list/falkordb/` | 具体目录待 Task 10 盘点 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/4405 | fixed | `BUG list/arcadedb/` | 具体目录待 Task 10 盘点 |
| Apache AGE | https://github.com/apache/age/issues/2428 | uninterested | `BUG list/apache_age/` | 具体目录待 Task 10 盘点 |
| LadybugDB | https://github.com/LadybugDB/ladybug/issues/668 | waiting reply | `BUG list/LadyBugDB/` | 具体目录待 Task 10 盘点 |
| LadybugDB | https://github.com/LadybugDB/ladybug/issues/669 | waiting reply | `BUG list/LadyBugDB/` | 具体目录待 Task 10 盘点 |
| Neo4j | https://github.com/neo4j/neo4j/issues/13813 | confirmed | `BUG list/neo4j/` | 具体目录待 Task 10 盘点 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/5116 | fixed | `BUG list/arcadedb/` | 具体目录待 Task 10 盘点 |
| ArcadeDB | https://github.com/ArcadeData/arcadedb/issues/5117 | fixed | `BUG list/arcadedb/` | 具体目录待 Task 10 盘点 |

状态计数：4 fixed、2 confirmed、2 uninterested、3 waiting reply。`waiting reply` 是观察期内的
右删失状态，而不是“未确认 bug”。Task 10 将为每条记录建立独立 evidence package、来源 hash
和缺失项清单；在此之前，禁止从本表推导 runtime 验证成功率或维护者确认率。

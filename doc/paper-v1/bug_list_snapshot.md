# GraphSymPerf 公开问题快照

> 快照日期：2026-07-22  
> `项目状态` 来自作者维护的 BUG LIST；`GitHub 状态` 于同日通过 GitHub API 复核。两者不是同一概念：前者表达开发者反馈语义，后者只表示 issue 当前 open/closed。原始列表中的 `comfirmed` 已统一为 `confirmed`，Apache AGE #2480 与 #2481 的互换链接已按编号纠正。

## 汇总

| 指标 | 数量 |
|---|---:|
| 公开报告 | 29 |
| 性能问题 | 25 |
| 逻辑问题 | 4 |
| fixed | 10 |
| confirmed | 5 |
| uninterested | 1 |
| waiting reply | 13 |
| Developer-Validated Bug（fixed + confirmed） | 15 |
| GitHub closed | 15 |
| GitHub open | 14 |

## 明细

| 数据库 | Issue | 类型 | 项目状态 | GitHub 状态 |
|---|---|---|---|---|
| Neo4j | [#13783](https://github.com/neo4j/neo4j/issues/13783) | Performance | fixed | closed |
| Neo4j | [#13814](https://github.com/neo4j/neo4j/issues/13814) | Performance | uninterested | closed |
| Memgraph | [#4208](https://github.com/memgraph/memgraph/issues/4208) | Performance | confirmed | open |
| Memgraph | [#4459](https://github.com/memgraph/memgraph/issues/4459) | Performance | waiting reply | open |
| FalkorDB | [#2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | Performance | waiting reply | open |
| ArcadeDB | [#4405](https://github.com/ArcadeData/arcadedb/issues/4405) | Performance | fixed | closed |
| ArcadeDB | [#5360](https://github.com/ArcadeData/arcadedb/issues/5360) | Logical | fixed | closed |
| ArcadeDB | [#5362](https://github.com/ArcadeData/arcadedb/issues/5362) | Performance | fixed | closed |
| Apache AGE | [#2428](https://github.com/apache/age/issues/2428) | Performance | confirmed | closed |
| LadybugDB | [#668](https://github.com/LadybugDB/ladybug/issues/668) | Performance | confirmed | closed |
| LadybugDB | [#669](https://github.com/LadybugDB/ladybug/issues/669) | Performance | confirmed | closed |
| LadybugDB | [#697](https://github.com/LadybugDB/ladybug/issues/697) | Logical | fixed | closed |
| LadybugDB | [#698](https://github.com/LadybugDB/ladybug/issues/698) | Performance | fixed | closed |
| Neo4j | [#13813](https://github.com/neo4j/neo4j/issues/13813) | Performance | confirmed | closed |
| Neo4j | [#13864](https://github.com/neo4j/neo4j/issues/13864) | Performance | waiting reply | open |
| Neo4j | [#13865](https://github.com/neo4j/neo4j/issues/13865) | Performance | waiting reply | open |
| Neo4j | [#13866](https://github.com/neo4j/neo4j/issues/13866) | Logical | waiting reply | open |
| Neo4j | [#13867](https://github.com/neo4j/neo4j/issues/13867) | Performance | waiting reply | open |
| ArcadeDB | [#5116](https://github.com/ArcadeData/arcadedb/issues/5116) | Performance | fixed | closed |
| ArcadeDB | [#5117](https://github.com/ArcadeData/arcadedb/issues/5117) | Performance | fixed | closed |
| ArcadeDB | [#5363](https://github.com/ArcadeData/arcadedb/issues/5363) | Performance | fixed | closed |
| LadybugDB | [#696](https://github.com/LadybugDB/ladybug/issues/696) | Logical | fixed | closed |
| LadybugDB | [#720](https://github.com/LadybugDB/ladybug/issues/720) | Performance | waiting reply | open |
| LadybugDB | [#721](https://github.com/LadybugDB/ladybug/issues/721) | Performance | waiting reply | open |
| Memgraph | [#4456](https://github.com/memgraph/memgraph/issues/4456) | Performance | waiting reply | open |
| Memgraph | [#4457](https://github.com/memgraph/memgraph/issues/4457) | Performance | waiting reply | open |
| Memgraph | [#4458](https://github.com/memgraph/memgraph/issues/4458) | Performance | waiting reply | open |
| Apache AGE | [#2480](https://github.com/apache/age/issues/2480) | Performance | waiting reply | open |
| Apache AGE | [#2481](https://github.com/apache/age/issues/2481) | Performance | waiting reply | open |

## 论文使用约束

1. `fixed` 与 `confirmed` 可合并为开发者验证问题，但必须保留各自类别。
2. `waiting reply` 是未决样本，不能放入正例或负例分母。
3. `uninterested` 表示维护者未采纳，不应自动解释为错误报告或预期行为。
4. 该列表跨越不同版本、规则族和验证协议，只支持累计外部效度，不支持当前正式实验的发现率或候选精度。
5. 状态会随上游进展变化；论文冻结实验结果时必须重新核对，并记录固定观察窗口。

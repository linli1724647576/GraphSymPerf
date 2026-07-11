# GDBMS 与 GDsmith 兼容性

## 原则

GDsmith 是唯一生成器，但并不等于所有目标都能作为 GDsmith 原生 backend。生成图/查询后必须经过目标 dialect 的 `CompatibilityDecision`：仅证明语义不变的 renderer 可重写；其他结构一律 `REJECT` 并 top-up accepted budget，不能删除标签、关系类型或谓词来“兼容”。

| 目标 | 生成边界 | 回放策略 |
| --- | --- | --- |
| Neo4j | GDsmith Neo4j backend | native Cypher adapter |
| Memgraph | GDsmith compatible backend | capability-checked Cypher |
| FalkorDB | GDsmith compatible backend | capability-checked Cypher |
| AgensGraph | GDsmith AgensGraph backend | SQL/Cypher envelope，single-label capability |
| Apache AGE | 隔离的 AgensGraph generation proxy | AGE SQL function/envelope 后在目标实例重放 |
| ArcadeDB | GDsmith compatible backend | ArcadeDB adapter 与受限 Cypher 能力 |
| LadybugDB | 隔离的 Neo4j generation proxy | 解析后的 graph snapshot 与 query 在 LadybugDB 重放 |

Apache AGE 的 proxy URI 使用 `GRAPHSYMPERF_GDSMITH_PROXY_APACHE_AGE_URI`，LadybugDB 使用 `GRAPHSYMPERF_GDSMITH_PROXY_LADYBUGDB_URI`。两者使用独立 proxy 实例、端口和图隔离命名，不与 AgensGraph 正式目标共用可破坏状态。proxy 只负责 GDsmith 生成边界，runtime 必须在目标 DBMS 上测量；resolved manifest 记录 `agensgraph_proxy` 或 `neo4j_proxy`，不能声称为目标原生生成覆盖。

AgensGraph 多标签不能退化为“只取第一个 label”；该结构必须被 capability gate 拒绝。Apache AGE/AgensGraph 的 SQL wrapper 只属于 adapter rendering，不进入 query equivalence 或 runtime oracle。

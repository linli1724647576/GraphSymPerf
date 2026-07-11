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
| ArcadeDB | GDsmith PostgreSQL-wire backend (`GRAPHSYMPERF_ARCADEDB_POSTGRES_URI`) | ArcadeDB HTTP adapter 与受限 Cypher 能力 |
| LadybugDB | 隔离的 Neo4j generation proxy | 解析后的 graph snapshot 与 query 在 LadybugDB 重放 |

Apache AGE 的 proxy URI 使用 `GRAPHSYMPERF_GDSMITH_PROXY_APACHE_AGE_URI`，LadybugDB 使用 `GRAPHSYMPERF_GDSMITH_PROXY_LADYBUGDB_URI`。两者使用独立 proxy 实例、端口和图隔离命名，不与 AgensGraph 正式目标共用可破坏状态。proxy 只负责 GDsmith 生成边界，runtime 必须在目标 DBMS 上测量；resolved manifest 记录 `agensgraph_proxy` 或 `neo4j_proxy`，不能声称为目标原生生成覆盖。

`systems.yaml` 中声明 `single_node_label` 能力的 AgensGraph、Apache AGE、ArcadeDB 和 LadybugDB 均会在各自 GDsmith 生成边界加入 `--label-num 1`，从生成空间源头约束为单标签；AgensGraph 兼容 JAR 仍保留多标签拒绝守卫，不能退化为“只取第一个 label”。GDsmith 默认字符串策略生成的双引号英数/下划线字面量仅在兼容 JAR 执行边界转换为 AgensGraph 单引号字面量；遇到其他双引号 token 必须失败关闭，不尝试处理转义或任意文本。

AgensGraph 与 ArcadeDB 的 GDsmith JDBC 连接在兼容 JAR 中对每个 statement 设置 60 秒查询超时；单条不可终止查询不应占满整个 shard 时间预算。外层 120 秒无进展 watchdog 仍作为 JDBC 驱动/服务端未按时取消时的第二道守卫。

Apache AGE/AgensGraph 的 SQL wrapper 只属于 adapter rendering，不进入 query equivalence 或 runtime oracle。

LadybugDB 的 label/property schema 不能在原文件上改变已有属性类型。每个 shard 加载新 GDsmith 图前，adapter 必须关闭连接并删除 `GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH` 指定的专用普通文件，然后从当前 graph snapshot 重建 schema。相对路径、符号链接和目录一律拒绝，避免扩大破坏边界。

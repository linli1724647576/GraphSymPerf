# 运维与故障恢复

## 命令面

主命令只有 `experiment run` 和 `experiment resume`。以下为 advanced commands：

```bash
graphsymperf experiment doctor --config config/experiment.yaml
graphsymperf experiment plan --config config/experiment.yaml
graphsymperf experiment gate --target <dbms> --accepted-pairs <10|100|1000>
graphsymperf experiment status --run-id <run-id>
graphsymperf issue validate --candidate <candidate-outcome.json>
graphsymperf issue reduce --candidate <candidate-outcome.json>
graphsymperf issue export --issue <submit-ready-index.json> --output <destination.json>
```

doctor 要求仓库与 GDsmith provenance 可核查、固定 commit 匹配、JAR 与 build manifest 的内容摘要匹配、7 个目标连接/driver/graph isolation 可用，并预留 112 × 500 MiB 的最大 artifact 空间。6 个容器型目标同时提供 `GRAPHSYMPERF_<DBMS>_CONTAINER` 与期望 `IMAGE_DIGEST`；doctor 使用 `docker inspect` 读取实际 `.Image` 并逐字匹配。LadybugDB 校验实际安装分发包的逐文件摘要。任一失败时 `experiment run` 返回非零且不启动部分矩阵。

## 卡死与自动退出

每次 adapter 操作前原子写 `inflight.json`，包含 query/graph/DBMS hash、query kind、开始时间、DBMS/driver operation timeout 与进程级 hard deadline。hard deadline 的取消传播宽限为 operation timeout 的 10%，最少 0.1 秒、最多 5 秒；它只用于等待 DBMS 取消和 driver 返回 typed timeout，不计入有效 runtime 样本。worker 在独立进程组中运行；heartbeat 过期或 hard deadline 后终止整个进程组。GDsmith 进程即使仍存活，若 raw/log 连续 120 秒无任何字节进展，也会终止其独立进程组；已有可回放语料标记为 `NO_PROGRESS_TIMEOUT` 并进入后续筛选，无可回放查询则失败关闭。当前 shard 对瞬态失败最多重试 3 次，其他 shard 继续；参数、证明或 artifact 不一致引发的确定性 `EVIDENCE_INTEGRITY_ERROR` 立即失败关闭，不做无效重试。query timeout、driver timeout、worker hung、worker crashed、DBMS crashed、evidence integrity error 与 infrastructure error 分开记录。

GDsmith raw 临时上限为 shard 预算的 70%，且最多 350 MiB；对正式 500 MiB shard 保留至少 150 MiB 给 checkpoint、accepted corpus 与候选证据。直接 stdout/stderr 不落入无界文件，runner 从 pipe 持续排空并只保留有界 head/tail；运行中也会删除非 `*-cur.log` 的 rotated DBMS logs，因为 parser 和最终 artifact 从始至终只使用 current log。raw 达到分配预算时会终止进程并标记 `RAW_ARTIFACT_BUDGET_REACHED`。只有 parser 已经从预算内文件提取到图快照与至少一个可回放查询时，该 partial corpus 才能继续 acceptance；空语料或普通非零退出仍失败关闭。supervisor 仍对最终 shard 强制 500 MiB 硬上限。

GDsmith query-record 请求量由 system capability 决定：声明 `low_yield_generation` 的 AgensGraph/Apache AGE 使用 `min(max_generated_seeds, accepted_target × 5)`，其他目标使用 `×2`。5倍系数来自 AgensGraph 实跑中 4,000 条仅得到 968 对的最弱已观测 yield；2倍路径避免在 Neo4j 等高 yield 目标上执行数小时的无效 oracle 生成。这一 capability 只调整生成容量，不改变规则权重或筛选门槛。`max_generated_seeds=10,000` 仍是 acceptance 硬停止边界；语料不足时 shard 只能为 `INCOMPLETE`，不会降低 accepted target。

GDsmith 的 compared 系列算法达到 `--num-queries` 后会以 `RuntimeException: total number reached` 和进程码 1 结束。runner 只在同时满足“原始进程码为 1、精确 sentinel 存在、parser 得到非空可回放语料”时将其记录为 `QUERY_RECORD_LIMIT_REACHED` 并继续；普通 code 1、伪 sentinel 或空语料仍失败关闭，不能触发确定性重试风暴。

兼容 JAR 对 AgensGraph 和 ArcadeDB 的每个 JDBC `Statement` 同时设置 60 秒 query timeout 与 1,000 行 result cap；对 Neo4j driver 的 write transaction 和 auto-commit `run` 设置 30 秒服务端 transaction timeout，为 60 秒生成停滞 watchdog 留出恢复余量。row cap 只约束 GDsmith 内部 JDBC oracle 的结果物化，不改变 GraphSymPerf 后续的 ordinary-execute runtime 协议；它防止高基数生成查询在 PostgreSQL JDBC `processResults` 中耗尽生成器 JVM heap。JAR manifest 必须同时声明 `jdbc_query_timeout_60s`、`jdbc_result_row_cap_1000` 与 `neo4j_transaction_timeout_30s`，否则 doctor 拒绝运行。

ArcadeDB 的 shard、独立 validation session 和 reducer 使用服务器级 `drop database`/`create database` 重建 `mydb`；这一边界避免对旧图执行 `MATCH (n) DETACH DELETE n` 时的全图扫描与 Java heap 放大。数据库名只接受受限标识符，重建任一步失败都不会继续加载或测量。其他 adapter 未声明 database-level reset 时仍使用 Cypher 清图，再执行各自的 schema reset。

Neo4j Community Edition 不能用 Enterprise-only database recreate 流程，因此其 adapter 使用 `CALL (n) { ... } IN TRANSACTIONS OF 1000 ROWS` 分批 `DETACH DELETE`，避免单一全图写事务占满 transaction memory/heap。该查询必须在 driver 的 implicit transaction 中运行，并继承同一 graph-reset hard deadline；超时仍失败关闭，不会在未知部分删除状态上开始测量。

resume 只接受同一 `resolved_manifest.json`；配置默认值变化、任务身份变化、artifact hash 不一致或 event sequence 断裂都会拒绝恢复。append-only accepted/candidate hash 防止重试重复计数。

## 候选审计

每个正式候选产生 shard 级 `candidate-outcomes/<candidate-hash>.json`，resume 会先复核并复用该终态，避免重新执行已完成的双 session/reducer。advanced validate/reduce 命令复核 candidate 内容寻址链并读取终态，不会把 raw JSON 重新解释为 credential。SubmitReadyIssue export 同样只复制运行时 typed exporter 已生成且 hash-chain 复核成功的文件。

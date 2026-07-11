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

每次 adapter 操作前原子写 `inflight.json`，包含 query/graph/DBMS hash、query kind、开始时间与 deadline。worker 在独立进程组中运行；heartbeat 过期或 hard deadline 后终止整个进程组。GDsmith 进程即使仍存活，若 raw/log 连续 120 秒无任何字节进展，也会终止其独立进程组；已有可回放语料标记为 `NO_PROGRESS_TIMEOUT` 并进入后续筛选，无可回放查询则失败关闭。当前 shard 对瞬态失败最多重试 3 次，其他 shard 继续；参数、证明或 artifact 不一致引发的确定性 `EVIDENCE_INTEGRITY_ERROR` 立即失败关闭，不做无效重试。query timeout、driver timeout、worker hung、worker crashed、DBMS crashed、evidence integrity error 与 infrastructure error 分开记录。

GDsmith raw 达到分配预算时会终止进程并标记 `RAW_ARTIFACT_BUDGET_REACHED`。只有 parser 已经从预算内文件提取到图快照与至少一个可回放查询时，该 partial corpus 才能继续 acceptance；空语料或普通非零退出仍失败关闭。这避免在固定 seed 上重复生成同一批超额日志。

ArcadeDB 的 shard、独立 validation session 和 reducer 使用服务器级 `drop database`/`create database` 重建 `mydb`；这一边界避免对旧图执行 `MATCH (n) DETACH DELETE n` 时的全图扫描与 Java heap 放大。数据库名只接受受限标识符，重建任一步失败都不会继续加载或测量。其他 adapter 未声明 database-level reset 时仍使用 Cypher 清图，再执行各自的 schema reset。

Neo4j Community Edition 不能用 Enterprise-only database recreate 流程，因此其 adapter 使用 `CALL (n) { ... } IN TRANSACTIONS OF 1000 ROWS` 分批 `DETACH DELETE`，避免单一全图写事务占满 transaction memory/heap。该查询必须在 driver 的 implicit transaction 中运行，并继承同一 graph-reset hard deadline；超时仍失败关闭，不会在未知部分删除状态上开始测量。

resume 只接受同一 `resolved_manifest.json`；配置默认值变化、任务身份变化、artifact hash 不一致或 event sequence 断裂都会拒绝恢复。append-only accepted/candidate hash 防止重试重复计数。

## 候选审计

每个正式候选产生 shard 级 `candidate-outcomes/<candidate-hash>.json`，resume 会先复核并复用该终态，避免重新执行已完成的双 session/reducer。advanced validate/reduce 命令复核 candidate 内容寻址链并读取终态，不会把 raw JSON 重新解释为 credential。SubmitReadyIssue export 同样只复制运行时 typed exporter 已生成且 hash-chain 复核成功的文件。

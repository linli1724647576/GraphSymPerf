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

每次 adapter 操作前原子写 `inflight.json`，包含 query/graph/DBMS hash、query kind、开始时间与 deadline。worker 在独立进程组中运行；heartbeat 过期或 hard deadline 后终止整个进程组。当前 shard 最多重试 3 次，其他 shard 继续。query timeout、driver timeout、worker hung、worker crashed、DBMS crashed 与 infrastructure error 分开记录。

resume 只接受同一 `resolved_manifest.json`；配置默认值变化、任务身份变化、artifact hash 不一致或 event sequence 断裂都会拒绝恢复。append-only accepted/candidate hash 防止重试重复计数。

## 候选审计

每个正式候选产生 shard 级 `candidate-outcomes/<candidate-hash>.json`，resume 会先复核并复用该终态，避免重新执行已完成的双 session/reducer。advanced validate/reduce 命令复核 candidate 内容寻址链并读取终态，不会把 raw JSON 重新解释为 credential。SubmitReadyIssue export 同样只复制运行时 typed exporter 已生成且 hash-chain 复核成功的文件。

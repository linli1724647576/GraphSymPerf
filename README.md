# GraphSymPerf

GraphSymPerf 使用 GDsmith 生成图与查询，并在可证明安全的 BOOL/SYM 变换后，以普通执行时间筛选图数据库性能异常。正式流程会对候选执行独立复核、成对查询归约、归约后再复核和去重；未完成这些门禁的结果不会导出给开发者。

## 主实验

唯一正式配置是 `config/experiment.yaml`，唯一启动命令是：

```bash
graphsymperf experiment run --config config/experiment.yaml
```

该配置展开为 7 个 GDBMS × 16 shards × 每 shard 1,000 个 accepted query pairs，共 112,000 对。命令先执行 doctor；任一数据库、GDsmith 固定提交、镜像摘要、驱动、隔离能力或磁盘预算不满足时，整轮实验不会部分启动。

中断后只恢复同一不可变 manifest：

```bash
graphsymperf experiment resume --run-id <run-id> --config config/experiment.yaml
```

## 结果位置

每次运行只写入新的 `runs/<run-id>/`：

- `resolved_manifest.json`：不可变的 112-task 计划；
- `shards/<dbms>-<shard>/`：accepted pairs、runtime candidates、validation、reduction、checkpoint 与可靠性证据；
- `issues/index/`：通过全部门禁的 SubmitReadyIssue 索引；
- `issues/submit-ready/`：由 typed exporter 生成的开发者侧摘要；
- `run_summary.json`：completed、incomplete、failed 计数。

`COMPLETED` 要求恰好 1,000 个 accepted pairs，并完成该 shard 所有 runtime candidates 的 validation/reduction 终态处理；999 对只能是 `INCOMPLETE`。

## Issue 生命周期

runtime candidate 不是 issue。只有 `VALIDATION_PASSED → REDUCED_VALID → SubmitReadyIssue` 且通过运行内去重的案例才可导出。提交到上游后才计为 Reported Issue Report；维护者明确 confirmed 或 fixed 后才计为 Developer-Validated Bug。详见 [issue 生命周期](docs/issue-lifecycle.md) 与 [主实验运行手册](docs/runbook-main-experiment.md)。

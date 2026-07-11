# 主实验运行手册

## 运行前

确认 7 个 GDBMS 版本和隔离 slot；GDsmith checkout 位于配置指定路径且 commit/dirty 状态满足 doctor；每个目标提供连接变量、`GRAPHSYMPERF_<DBMS>_CONTAINER=<name-or-id>` 及 `GRAPHSYMPERF_<DBMS>_IMAGE_DIGEST=sha256:<64 hex>`，两者须经 Docker 实测匹配；磁盘至少覆盖 112 × 500 MiB 上限。先完成每 DBMS 1×10、1×100、1×1,000 递进 gate。

advanced 预检：

```bash
graphsymperf experiment doctor --config config/experiment.yaml
graphsymperf experiment plan --config config/experiment.yaml
```

plan 必须输出 systems=7、shards=112、accepted_pair_budget=112000，且 manifest 有 112 个唯一 seed。

## 唯一正式命令

```bash
graphsymperf experiment run --config config/experiment.yaml
```

不在重构或清理提交中自动启动长跑。该命令创建全新 run ID，不读取旧 `runs/`、`logs/` 或旧结果。

## 恢复与状态

```bash
graphsymperf experiment resume --run-id <run-id> --config config/experiment.yaml
graphsymperf experiment status --run-id <run-id>
```

resume 必须使用原配置解析出完全相同的 immutable manifest。`INCOMPLETE` 表示 accepted count 或阶段链不足，应检查 `checkpoint.json`、`failure.json`、`events.jsonl`、`artifact_warnings.jsonl` 和最后 attempt；不能手工修改计数后恢复。

## 结果核对

每 shard 检查 `accepted_pairs.jsonl`、`candidates.jsonl`、`attempt-*/validation.json`、`attempt-*/reduction.json` 和 `candidate-outcomes/`。validation/reducer 的 content-addressed 对象在 shard `artifacts/`；SubmitReadyIssue 索引与 typed export 在 run 级 `issues/index/`、`issues/submit-ready/`。ReliabilityFinding 单独位于 run 级可靠性记录，不计性能 issue。

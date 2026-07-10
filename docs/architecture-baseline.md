# Runtime-first 重构实施基线

记录日期：2026-07-10。基线提交为 `1c0ed2e`。本文件只记录该提交的真实状态，
不把尚未接入主链的模块写成已完成能力。

## 可核查基线

- 干净工作树全量测试：137 passed；其中 unit 113、contracts 7。
- 正式配置文件：`config/experiment.yaml`；辅助表为 `config/systems.yaml` 与
  `config/rules.yaml`。
- 正式命令实现：`graphsymperf` 与兼容别名 `graph-sym-perf` 均指向同一个
  runtime-first CLI。
- 计划展开：7 个 GDBMS、每系统 16 shards、每 shard 1000 accepted pairs，
  共 112 tasks 和 112,000 accepted-pair budget。
- 目录体积（2026-07-10 快照）：仓库约 3.2 MB，`src/` 约 1.6 MB，`tests/`
  约 696 KB，`BUG list/` 约 588 KB；未复制旧运行产物。

## 已形成的分层

仓库已删除旧 `app/mining.py`、`experiment/runner.py`、PMC feedback 和 plan-oracle
实现，并建立 experiment、generation、workload、equivalence、runtime、validation、
reducer、dialects、artifacts 与 issues 等包。契约测试要求 runtime、equivalence 和
transforms 不反向依赖 CLI、app、experiment 或具体 adapter；experiment 不得再依赖
已删除的 app 私有函数。

## 仍未形成的生产闭环

该基线只有 generation、accepted-pair 构建和 discovery runtime screen 接入 worker。
独立双 session validation、paired reducer、reduced-pair 再验证、typed promotion、
issue exporter、registry、内容寻址存储、heartbeat/resume 仍未完整接入生产主链。
因此“模块存在”不等于“全量实验结果可信”。

## 用户工作树保护

原主工作区在实施开始时包含以下未提交路径，独立工作树不得恢复或覆盖它们：

- 已删除：`BUG list/BUG001_replay_summary.json`
- 已删除：`BUG list/README.md`
- 已删除：`BUG list/reproducer_graph.cypher.txt`
- 已修改：`tests/unit/test_agensgraph_compat_builder.py`
- 未跟踪：`docs/superpowers/`

本次实现位于独立工作树和 `codex/runtime-first-full` 分支；不对上述主工作区路径执行
checkout、reset 或自动恢复。

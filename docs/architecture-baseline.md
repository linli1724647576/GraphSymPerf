# Runtime-first 重构前架构基线

记录日期：2026-07-10。本文件是迁移基线，不是新实现的架构规范。

当前主链路以 `graph-sym-perf` CLI、`app/mining.py`、`experiment/runner.py` 和旧
manifest 投影为中心；生成、PROFILE/PMC、确认、聚类和报告仍紧耦合。`experiment/runner.py`
直接依赖 `app.gdsmith_mining` 的私有函数，这是 Task 6 与 Task 11 必须移除的显式遗留例外。

当前 `equivalence/` 与 `transforms/` 已不依赖 CLI、app、experiment 或具体 adapter；它们被
`tests/contracts/test_layer_boundaries.py` 保护。Task 3--10 新增模块必须继续保持这个方向，最终
架构将由 `docs/architecture.md` 取代本文件。

基线入口包括 `graph-sym-perf run-ldbc-mining`、`run-gdsmith-mining`、`experiment` 和
`runtime-check`。其中 LDBC、PROFILE/PMC 和多份实验 YAML 都是待迁移遗留能力，不能被误写为
runtime-first 正式实验的未来接口。

实施开始时的工作树包含用户已修改和已删除的路径，完整路径快照保存在本机临时实施日志
`/tmp/graphsymperf-status-before-runtime-first-refactor.txt`；实施期间不会用 checkout、reset 或
自动恢复它们。

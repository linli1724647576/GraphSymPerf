# Task 8 实施报告

## 结论

已实现 Stage-2 候选的双进程独立复验、relative/absolute OR 提交路径、同侧 timeout 诊断确认，以及 CONTROL 固定抽样诊断。验证结果由候选、Stage-2 evidence、entry/stream、graph 与 DBMS fingerprint 共同绑定；CONTROL 不计入 discovery promotion、主验证通过数或 SubmitReady 指标。

## 核心实现

- `SpawnedValidationSessionRunner` 使用 `spawn` 启动两个新进程；每个进程创建独立 adapter/connection，清空并重载 graph，重算 graph/DBMS fingerprint，执行完整 typed equivalence、A/A 与 B/B 负控、5 次 warmup 和 20 个平衡 paired samples，并在 `finally` 关闭 adapter。
- 两个 measurement schedule seed 由 `candidate_hash + session_number` 确定性派生；父进程 hard timeout 时直接 kill 子进程，空队列和子进程异常均 fail closed。
- pooled gate 固定使用 40 个 paired samples 和 10,000 次 bootstrap：
  - relative：每 session ratio >= 2、pooled ratio >= 3、ratio CI low >= 2、delta > effective noise；
  - absolute：每 session delta > 0、pooled delta >= max(5 * effective noise, 50 ms)、delta CI low > effective noise、pooled ratio >= 1.5、ratio CI low >= 1.2；
  - 两条均通过时确定性选择 relative，并记录 `absolute_also_passed=true`。
- timeout 仅在两个独立 session 同侧全部 timeout、另一侧全部完成、graph/DBMS/health/lifecycle/connection identity 均通过时记录 `timeout_validation_confirmed`；结果保持 `INCONCLUSIVE`，`submission_path=None`。
- discovery 只复验 Stage-2 promote candidates；CONTROL 有 gate hit 时最多选 2 个，否则选 noise-normalized score 最高的 1 个，只输出 `control_gate_hit/control_validation_pass` 诊断。
- 删除 validation 内重复的 `SafetyCertificate`，统一使用 transforms canonical model；相关 validation、reducer 与 artifact schema 夹具同步迁移。
- validation artifact 封存 submission path、pooled delta/CI、noise policy 和 metadata；未修改通用 `ArtifactStore`，absolute SubmitReady/reduction 制品留给 Task 9。

## TDD 与验证证据

- 初始 RED：缺少 `SubmissionPath` 与 `select_control_validation_pairs`；随后同进程拒绝、显式 OR path、spawn lifecycle、canonical certificate、worker resume、timeout fail-closed 分批转绿。
- timeout 补强 RED：partial timeout 与非 timeout 侧未完成曾被错误确认；修复后 8 个 timeout focused cases 通过。
- validation artifact RED：absolute report 被旧 AND schema 拒绝；修复后 relative/absolute validation artifact 均可封存。
- 简报 focused：`247 passed in 86.49s`。
- 全仓首轮：`904 passed, 2 failed`，失败均为旧手工 artifact fixture 缺少 Task 8 pooled/path/noise 字段；升级为 canonical fixture 后定点 `2 passed`。
- 全仓终轮：`906 passed in 291.33s`。
- `python -m compileall -q src tests`：通过。
- `git diff --check`：通过。

## 边界与后续

- Task 8 不创建或物化新的 SubmitReadyIssue；worker summary 的 `submit_ready_issues` 固定为 0。
- 通用 ArtifactStore 仍按既有 Task 9 物化边界处理 reduction/issue；本任务未修改该文件。
- 未引入 PROFILE、query plan 或 PMC 作为 runtime oracle。

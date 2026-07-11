# GraphSymPerf 项目上下文

## 研究目标

GraphSymPerf 研究图数据库管理系统（Graph Database Management System, GDBMS）在语义等价查询上的显著运行时间差异。核心贡献是“安全点驱动的 BOOL/SYM 变换 + 运行时间优先判定”，而不是查询计划差异检测器。

## 固定决策

- 生成器只有 GDsmith；不存在其他生成器 fallback。
- 正式矩阵只有 `config/experiment.yaml`：7 × 16 × 1,000 accepted query pairs。
- ordinary execute runtime 是 discovery、validation 和 reducer 的唯一性能症状来源。
- plan、PROFILE 与 PMC 只能在 reduced pair 已复核后解释现象，不能改变任何 promotion gate。
- Apache AGE、AgensGraph 和 LadybugDB 的生成/回放边界必须显式记录；不允许有损方言降级。
- 进程卡死、驱动不可取消、DBMS 重启、OOM 和连接断开形成 ReliabilityFinding，不混入 runtime issue。
- 原始 pair 与 reduced pair 都必须通过两个独立 session；否则不得自动 export。

## 成功口径

单个 shard 的完成口径是精确 accepted-pair count 与完整阶段证据，不是“进程退出码为 0”。论文报告需分别给出候选数、validation 通过数、REDUCED_VALID 数、Reported Issue Report 数和 Developer-Validated Bug 数；累计公开案例不能替代单次、版本固定的无偏 yield。

## 事实源

配置事实来自 `config/experiment.yaml`、`config/systems.yaml` 和 `config/rules.yaml`；状态事实来自 `runs/<run-id>/resolved_manifest.json` 与内容寻址 artifact。README 是入口摘要，`docs/` 解释协议，不得另建第二套正式配置或旧版流程文档。

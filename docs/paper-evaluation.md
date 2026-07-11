# 论文评估设计

第一贡献固定为 safety-point 驱动的 BOOL/SYM 语义保持变换与 runtime-first oracle。validation、reducer、方言层和可靠运行是可信证据链，不单独包装成多个互不相关贡献。

## Research Questions

- RQ1 Effectiveness：每固定 accepted-pair budget 找到多少独立 validation 后仍显著的 anomaly？
- RQ2 Precision：screen 候选经 original validation、reducer、reduced validation 的 survival funnel 与拒绝原因如何？
- RQ3 Rule utility：各 CORE rule 的 accepted coverage、quarantine、validated yield 与 DBMS 分布如何？
- RQ4 Reduction：字符数、AST 节点数、归约尝试、1-minimal 成功率与 runtime 保留程度如何？
- RQ5 Generality：同一协议在 7 个 GDBMS 和不同方言能力上的覆盖、拒绝与 issue 分布如何？

## 必报分母与版本

每个结果绑定 repository、GDsmith、rule schema、validator、reducer、DBMS、driver、image digest、CPU、run manifest 与 seed。按 DBMS/rule 报 accepted pairs、runtime candidates、VALIDATION_PASSED、REDUCED_VALID、SubmitReadyIssue 和 SUBMITTED，不能只展示成功案例。

累计 30+ Reported Issue Reports 是跨版本 corpus 目标，不等于某轮 112,000 accepted pairs 的无偏 yield。现有 11 与新案例分组归因；历史 artifact 缺失字段写 unknown reason，禁止补造 v3 复核结果。

## 替代解释

比较时控制缓存顺序、A/A noise、结果截断、类型/重复数、graph state、DBMS restart、timeout 与多重筛选。plan/PROFILE/PMC 只能在通过 reduced validation 后做 root-cause hypothesis；不能用解释信号把 validation 失败候选改成 positive。

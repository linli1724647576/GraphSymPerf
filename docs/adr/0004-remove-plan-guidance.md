# ADR 0004：移除查询计划引导

- 状态：Accepted
- 日期：2026-07-10

## 背景

BOOL/SYM 的可证明语义空间有限，使用查询计划反馈改变 rule/site 采样会增加状态和解释泄漏，却不强化等价性或 runtime 真实性。

## 决策

删除查询计划引导、反馈采样和对应正式配置。scheduler 只使用 accepted count 与 completed-validation yield，并保留每规则 exploration floor。plan/PROFILE/PMC 留在可选解释层。

## 后果

实验更易复现，采样分母可审计；可能失去基于计划 novelty 的发现加速，但不会牺牲独立 validation 门槛。

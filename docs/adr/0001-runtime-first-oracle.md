# ADR 0001：Runtime-first Oracle

- 状态：Accepted
- 日期：2026-07-10

## 背景

计划差异、PROFILE elapsed 与 PMC novelty 能提示优化器行为，但可能与用户可见 runtime 无关，也可能受观测开销影响。

## 决策

discovery、validation 与 reducer 的性能判据只消费普通 `execute` 的 randomized paired timing。开发者侧比值只来自独立 validation。解释证据在 reduced pair 复核通过后按需采集。

## 后果

候选发现速度可能降低，但 oracle 与用户现象一致；profile-only/plan-only 项不会 promotion。RuntimeHarness 接口不暴露 profile 方法，并由依赖契约测试保护。

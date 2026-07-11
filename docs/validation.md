# 独立 Validation 协议

## 为什么独立

从大量候选中挑最大 runtime ratio 会产生 winner's curse。validation 不复算 discovery 样本，而是在两个全新 OS 进程中分别创建连接、清空并重载同一 graph snapshot；session ID、PID 与 connection identity 必须互不相同。

## 每个 Session

1. 校验 candidate hash、query/parameter/graph/config/policy/certificate hash、DBMS 与 driver 版本、镜像摘要和 CPU 架构。
2. reset/load 后读取节点、边、标签、类型和属性，graph fingerprint 必须等于候选绑定值。
3. purity gate 拒绝写查询、volatile function、未固定参数、无确定顺序的 LIMIT/SKIP/collect 和不支持的方言结构。
4. 两侧执行完整 typed result equivalence；截断、类型差异、NULL/missing、重复数或顺序语义无法确认时 fail closed。
5. 执行 A/A 与 B/B 的结果稳定性和 runtime 负对照。
6. 各 5 次 warmup，随后 20 个 balanced randomized pairs，AB/BA 各 10 次。
7. 连接关闭、子进程正常退出且期间无 health event。

## Promotion Gate

两个 session 必须同方向；合并样本的 median ratio ≥ 3.0，95% bootstrap CI lower ≥ 2.0，median absolute delta 大于 A/A noise p99。任何一个 session 不完整、fingerprint 漂移、方向相反或基础设施事件都不能得到 `VALIDATION_PASSED`。

validation ratio 是开发者侧可报告的数值。discovery ratio 只保留为历史筛选字段；缩小但仍过独立门槛时标为 `shrunk_but_real`，不得继续引用更大的 discovery 数值。

reducer 输出的新 pair 必须从零执行同一协议。原始 validation 不能替代 reduced validation。

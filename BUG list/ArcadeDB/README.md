# ArcadeDB issues

报告按实际 Pattern 分类；`Native/` 保存尚未纳入公共查询骨架的原生机制补测，贡献单独统计。

| Pattern／原生机制 | 报告 | 问题 |
| --- | --- | --- |
| P4 | [BUG001](<P4/BUG001.md>) | COUNT subquery runs before a false AND condition |
| S7 | [BUG002](<S7/BUG002.md>) | COALESCE evaluates a COUNT after a non-null constant |
| S8（Gremlin） | [BUG003](<S8/BUG003.md>) | Gremlin materializes all matching neighbors before an existence test |
| S5（Gremlin） | [BUG004](<S5/BUG004.md>) | Gremlin scans neighbors before an always-empty filter |

[总索引](../README.md)。BUG 编号在本数据库内稳定；报告数量不等于已证明的独立源代码根因数。

## Gremlin 现有 Pattern 检查（2026-10-18）

ArcadeDB 26.10.2 的 Gremlin 接入及接口门禁已通过。本轮只使用已有 Pattern，性能差异按 5 倍、置信下界 5 倍、绝对差值超过 100 ms、方向一致性及噪声门槛复核，并要求两次独立确认分别满足倍率和差值门槛。P10 完成 24 个扩大规模／种子输入，最大图为 1,500,003 个顶点，最大中位倍率只有 1.30585×，故仍不登记 P10 BUG。

S1–S10 的 180 个主矩阵输入完成后，两个根因通过缩减及原存档重放：BUG003 的 A/B 配对中位倍率为 **26.66×**、差值 **154.79 ms**；BUG004 为 **65.46×**、**201.98 ms**。S6 的邻居物化线索与 S8 合并，不另编号。两份报告的独立建图代码、完整原生结果和索引均已实测核验，状态为本地复现，**未提交上游**。

首轮未报原因见 [Gremlin 实验记录](../../docs/gremlin-experiment-20261018.md) 与 [逐案例计划审查](../../docs/gremlin-optimizer-audit-20261018.md)；本次扩规模、S 模板接口检查、失败记录、严格复算及报告依据见 [续测记录](../../docs/gremlin-p10-scor-20261018.md)。

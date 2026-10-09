# FalkorDB issues

报告按实际 Pattern 分类；`Native/` 保存尚未纳入公共查询骨架的原生机制补测，贡献单独统计。

| Pattern／原生机制 | 报告 | 问题 |
| --- | --- | --- |
| P10 | [BUG002](<P10/BUG002.md>) | Cartesian product scans millions of targets before an empty index lookup |
| P13 | [BUG001](<P13/BUG001.md>) | Repeated traversal after swapping independent OPTIONAL MATCH clauses |

[总索引](../README.md)。BUG 编号在本数据库内稳定；报告数量不等于已证明的独立源代码根因数。

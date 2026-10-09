# Apache AGE issues

报告按实际 Pattern 分类；`Native/` 保存尚未纳入公共查询骨架的原生机制补测，贡献单独统计。

| Pattern／原生机制 | 报告 | 问题 |
| --- | --- | --- |
| Native / correlated-count | [BUG001](<Native/correlated-count/BUG001.md>) | Correlated COUNT rescans the same leaf table for each root |
| Native / property-index | [BUG002](<Native/property-index/BUG002.md>) | Redundant COALESCE prevents a property index lookup |

[总索引](../README.md)。BUG 编号在本数据库内稳定；报告数量不等于已证明的独立源代码根因数。

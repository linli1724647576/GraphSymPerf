# JanusGraph issues

报告按实际 Pattern 分类；`Native/` 保存尚未纳入公共查询骨架的原生机制补测，贡献单独统计。

| Pattern／原生机制 | 报告 | 问题 |
| --- | --- | --- |
| Native / filter-wrapper | [BUG001](<Native/filter-wrapper/BUG001.md>) | A wrapped vertex filter misses early rejection |
| Native / unique-singleton | [BUG002](<Native/unique-singleton/BUG002.md>) | A singleton within predicate misses unique-index adjacency optimization |
| P3 | [BUG003](P3/BUG003.md) | Path-node checks do not prune failed repeat prefixes |
| S5 | [BUG004](S5/BUG004.md) | A constant-false child filter still expands every neighbor |
| S6（S8 同因支持） | [BUG005](S6/BUG005.md) | LIMIT(1) still preloads the complete adjacency list |

[总索引](../README.md)。BUG 编号在本数据库内稳定；报告数量不等于已证明的独立源代码根因数。

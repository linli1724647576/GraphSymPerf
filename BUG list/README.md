# Missed optimization issues

这里保留已完成完整结果语义核查、独立重建复现，并有原生执行计划证据解释额外工作的优化遗漏报告。

后续新增、修改或润色 issue 报告，必须遵循本目录的 [报告写作规则](AGENTS.md)。规则覆盖本目录及所有子目录，样稿为下面两份 Neo4j 报告。

## Neo4j（既有报告）

- [Neo4j BUG001：镜像路径丢失节点谓词剪枝](Neo4j/P3/BUG001.md)（P3，两种锚点写法合并）。
- [Neo4j BUG002：廉价 AND 条件晚于图子查询执行](Neo4j/P4/BUG002.md)（P4，EXISTS 与 COUNT>0 输入合并）。

## 本轮新增报告（2026-10-09）

本轮新增 **14 份报告，7 个非 Neo4j 数据库各 2 份**。Neo4j 两份既有报告未纳入本轮重测数量。

目录按 `数据库/Pattern/BUGxxx.md` 分类。原生补测按 `数据库/Native/机制名/BUGxxx.md` 分类，单独统计，不并入 P1–P16 或 S1–S10 的命中。BUG 编号在数据库内保持稳定；同一问题的后续案例继续使用原编号。

以下报告在两次独立重建中完成了 **40 组普通查询配对**，预热后读取完整结果，并单独采集原生执行计划。登记门槛为：配对耗时比中位数 **≥5×**、配对耗时差中位数 **>100 ms**，且通过同查询噪声控制。倍率和时间差按配对样本计算，不能用两项耗时中位数的商替代。

这些条目的状态是**本地复现（reproduced）**，尚无**上游开发者确认（upstream-confirmed）**。独立源代码根因需要按后续诊断和去重另行统计，不能由报告文件数量推算。具体版本、语义前提、完整建图、计时和计划见各报告；数值来源见[本轮核验清单](../results/detection-20261009/issue_inventory.json)。

### Memgraph

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](Memgraph/P11/BUG001.md) | P11 | MATCH 顺序导致对共享节点重复展开高分支 | 5.53×（q2/q1） | 145.67 ms |
| [BUG002](Memgraph/P10/BUG002.md) | P10 | Cartesian 在空索引查询前扫描独立大分支 | 165.54×（q2/q1） | 134.75 ms |

### FalkorDB

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](FalkorDB/P13/BUG001.md) | P13 | 独立 OPTIONAL MATCH 分支重复执行相关遍历 | 7.91×（q1/q2） | 162.40 ms |
| [BUG002](FalkorDB/P10/BUG002.md) | P10 | Cartesian Product 在空索引查询前扫描目标表 | 236.01×（q2/q1） | 123.63 ms |

### ArcadeDB

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](ArcadeDB/P4/BUG001.md) | P4 | COUNT 子查询在可判定为 false 的 AND 条件前执行 | 191.63×（q2/q1） | 224.52 ms |
| [BUG002](ArcadeDB/S7/BUG002.md) | S7 | COALESCE 计算非 NULL 常量之后未使用的 COUNT | 218.08×（q1/q2） | 219.12 ms |

### Apache AGE

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](<Apache AGE/Native/correlated-count/BUG001.md>) | Native / correlated-count | 相同关联键的 COUNT 子计划反复扫描叶节点表 | 15.13×（q1/q2） | 277.16 ms |
| [BUG002](<Apache AGE/Native/property-index/BUG002.md>) | Native / property-index | 多余 COALESCE 包装阻止已有属性索引访问 | 299.59×（q1/q2） | 145.46 ms |

### LadybugDB

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](LadybugDB/Native/coalesce-count/BUG001.md) | Native / coalesce-count | COALESCE 在非 NULL 常量之后执行未使用的 COUNT | 744.88×（q1/q2） | 131.28 ms |
| [BUG002](LadybugDB/Native/path-pruning/BUG002.md) | Native / path-pruning | ALL 节点条件在递归路径展开后过滤 | 307.99×（q1/q2） | 605.53 ms |

### JanusGraph

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](JanusGraph/Native/filter-wrapper/BUG001.md) | Native / filter-wrapper | filter 包装后的顶点条件未提前排除无效遍历 | 201.40×（q1/q2） | 294.92 ms |
| [BUG002](JanusGraph/Native/unique-singleton/BUG002.md) | Native / unique-singleton | 单元素 within 未触发唯一索引的邻接优化 | 176.70×（q1/q2） | 297.03 ms |

### HugeGraph

| 报告 | Pattern／原生机制 | 具体表现 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG001](HugeGraph/Native/filter-wrapper/BUG001.md) | Native / filter-wrapper | filter 包装后的顶点条件未提前排除无效遍历 | 100.93×（q1/q2） | 145.01 ms |
| [BUG002](HugeGraph/Native/count-nonnull/BUG002.md) | Native / count-nonnull | 冗余非 NULL 判断阻止 COUNT>0 的存在性优化 | 122.69×（q1/q2） | 248.31 ms |

报告为可直接提交的英文书信。仅凭耗时差异的候选、证据不足条目、阴性案例、正常求值差异及工程复现包已移出本目录；原始实验资料单独归档，不改变历史候选统计。

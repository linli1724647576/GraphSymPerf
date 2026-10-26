# Missed optimization issues

这里保留已完成完整结果语义核查、独立重建复现，并有原生执行计划证据解释额外工作的优化遗漏报告。

项目总账为根目录的 [bugs.csv](../bugs.csv)，长期维护所有已登记上游 issue、BUG 类型和状态，以及未提交的本地记录。以下目录按实验 Pattern 分类；历史上游 issue 没有可靠输入关联时，不从标题猜测 Pattern。

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

## Gremlin 续测新增报告（2026-10-18）

本次仅使用既有 Pattern 和方法，在 ArcadeDB 26.10.2 上新增两份本地复现报告，未提交上游；不计入上方 2026-10-09 的 14 份历史报告。两份报告均通过独立重建、缩减、原存档重放及报告建图脚本实测，倍率与差值取最终 40 对普通查询重放。P10 的 24 个扩规模／种子输入未达到阈值，未登记。

| 报告 | Pattern | 源码与计划支持的问题 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [BUG003](ArcadeDB/S8/BUG003.md) | S8 | 首次匹配前已完整包装邻居，阻止上层早停节省内部工作 | 26.66×（q1/q2） | 154.79 ms |
| [BUG004](ArcadeDB/S5/BUG004.md) | S5 | 恒空子过滤未消除前面的纯读邻居扫描 | 65.46×（q1/q2） | 201.98 ms |

S6 的相关线索与 S8 是同一桥接物化根因，不另增报告。完整统计和未通过案例见 [P10 与 S1–S10 续测记录](../docs/gremlin-p10-scor-20261018.md)。

## P3／P13／P16 编译扩展后的报告（2026-10-18）

补齐三个既有 Pattern 的 Gremlin 编译后，JanusGraph 与 HugeGraph 各完成 56 个既定输入。下列三份本地优化请求通过原阈值、缩减和独立重放；报告中的完整建图与原查询又在全新专用容器通过全图、完整结果及原生 PROFILE 检查。倍率与差值均取最终重放的 40 对普通查询，不使用 PROFILE 耗时。

| 报告 | Pattern／图态 | 原生计划支持的额外工作 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [JanusGraph BUG003](JanusGraph/P3/BUG003.md) | P3 / pruning_path | 路径前缀已经含 keep=false，Repeat 仍继续生成不可能通过的分支组合 | 13.91×（q2/q1） | 169.03 ms |
| [HugeGraph BUG003](HugeGraph/P3/BUG003.md) | P3 / pruning_path | 同上；父／子边 schema 限同一服务器进程验证 | 16.04×（q2/q1） | 141.20 ms |
| [HugeGraph BUG004](HugeGraph/P13/BUG004.md) | P13 / multi_low | LOW 扩行后，对同一 anchor 重复执行独立 HIGH 遍历 | 7.34×（q1/q2） | 124.92 ms |

两库的 P3 属于同一跨步骤前缀剪枝机会；快侧通过较少起点避免组合，并未实际应用前缀剪枝。P13 与已有 FalkorDB P13 属于相同逻辑机制在另一数据库上的复现。三份报告对应本轮两个优化机会族，不能当作三个独立源码根因；A/B 倍率也不是已实现补丁的收益。状态均为本地复现，未提交上游。

P3 original 的起点搜索差异、P16 的必需列表构造／正常短路不登记；JanusGraph P13 在本轮规模内未通过原阈值。完整记录见 [扩展与验证结果](../docs/gremlin-extended-20261018.md)，逐份来源见 [报告清单](../results/gremlin-extended-20261018/report-manifest.json)。

## JanusGraph S1–S10 续测报告（2026-10-19）

按用户调整，本阶段停止 HugeGraph，只继续 JanusGraph。既有 S1–S10 先通过语义与接口门禁，再完成 164 个原计划输入；另有 4 个条件输入未执行，不记为阴性。以下两份新增本地报告通过原阈值、有限缩减、独立重放、原始样本重算及空库全图与原生计划核验，未提交上游。

| 报告 | Pattern | 原生计划支持的额外工作 | 配对倍率 | 配对时间差 |
| --- | --- | --- | ---: | ---: |
| [JanusGraph BUG004](JanusGraph/S5/BUG004.md) | S5 | 恒假常量 child 仍在展开后对 20,000 个邻居逐一求值 | 70.21×（q1/q2） | 109.14 ms |
| [JanusGraph BUG005](JanusGraph/S6/BUG005.md) | S6（S8 同因支持） | 上层只消费少量结果，邻接查询仍预先返回 400,000 个 entries | 184.04×（q1/q2） | 254.46 ms |

倍率与差值来自各选中独立重放的 40 对普通查询；PROFILE 仅解释工作量。S8 的选中重放也通过门槛，但与 S6 走同一 EntryList 邻接预加载路径，合并为 BUG005 的支持证据。S5 零上限输入因差值未超过同查询噪声而不登记。报告数量不等于独立上游源码根因数，也不把这些机会族视为跨库新机制。

完整覆盖、阴性与未确认输入见 [实验记录](../docs/gremlin-scor-janus-huge-20261018.md)，逐份资格证据见 [本地报告记录](../results/gremlin-scor-janus-huge-20261018-v5/report-validation/local-report-publication.json)。

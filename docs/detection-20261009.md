# 2026-10-09 真实数据库优化遗漏实验

本轮使用专用本地实例，在 7 个已接入的非 Neo4j 目标中各登记 2 份报告，共 14 份；其中 6 个内置 Pattern 案例、8 个原生机制补测。报告只登记完整结果一致、普通查询计时通过门槛、原生计划显示可避免工作且经过人工核查的案例。

## 已完成的报告

BUG list 按 `数据库/Pattern/报告` 分类。原生补测放在 `数据库/Native/机制名/报告` 下，单独计数。Neo4j 既有两稿只调整目录，未纳入本轮重测统计。

下表为实际配对统计。倍率是每组慢／快查询比值的中位数；差值是每组慢减快的中位数，不是两项中位耗时的商或差。

| 数据库 | 报告 | Pattern／原生机制 | 配对倍率 | 配对差值 |
| --- | --- | --- | ---: | ---: |
| Apache AGE | [BUG001](<../BUG list/Apache AGE/Native/correlated-count/BUG001.md>) | native:correlated-count | 15.13× | 277.16 ms |
| Apache AGE | [BUG002](<../BUG list/Apache AGE/Native/property-index/BUG002.md>) | native:property-index | 299.59× | 145.46 ms |
| ArcadeDB | [BUG001](<../BUG list/ArcadeDB/P4/BUG001.md>) | P4 | 191.63× | 224.52 ms |
| ArcadeDB | [BUG002](<../BUG list/ArcadeDB/S7/BUG002.md>) | S7 | 218.08× | 219.12 ms |
| FalkorDB | [BUG002](<../BUG list/FalkorDB/P10/BUG002.md>) | P10 | 236.01× | 123.63 ms |
| FalkorDB | [BUG001](<../BUG list/FalkorDB/P13/BUG001.md>) | P13 | 7.91× | 162.40 ms |
| HugeGraph | [BUG002](<../BUG list/HugeGraph/Native/count-nonnull/BUG002.md>) | native:count-nonnull | 122.69× | 248.31 ms |
| HugeGraph | [BUG001](<../BUG list/HugeGraph/Native/filter-wrapper/BUG001.md>) | native:filter-wrapper | 100.93× | 145.01 ms |
| JanusGraph | [BUG001](<../BUG list/JanusGraph/Native/filter-wrapper/BUG001.md>) | native:filter-wrapper | 201.40× | 294.92 ms |
| JanusGraph | [BUG002](<../BUG list/JanusGraph/Native/unique-singleton/BUG002.md>) | native:unique-singleton | 176.70× | 297.03 ms |
| LadybugDB | [BUG001](<../BUG list/LadybugDB/Native/coalesce-count/BUG001.md>) | native:coalesce-count | 744.88× | 131.28 ms |
| LadybugDB | [BUG002](<../BUG list/LadybugDB/Native/path-pruning/BUG002.md>) | native:path-pruning | 307.99× | 605.53 ms |
| Memgraph | [BUG002](<../BUG list/Memgraph/P10/BUG002.md>) | P10 | 165.54× | 134.75 ms |
| Memgraph | [BUG001](<../BUG list/Memgraph/P11/BUG001.md>) | P11 | 5.53× | 145.67 ms |

## 判据与测量

- 配对中位倍率 ≥5，配对中位绝对差值 >100 ms；两次独立确认各自满足这两个条件。
- 两次独立重建，各 20 组平衡 AB／BA 普通查询执行，总计 40 组。每次确认的预热次数以各报告和原始记录为准。
- 完整消费并核对结果多重集，保留重复行、NULL、空结果与返回类型。计时不包含构图、建立连接或 PROFILE。
- 每次确认包含各 10 组 A／A、B／B 对照；配对时间差还须超过实测同查询噪声。95% 配对倍率 bootstrap 区间下界 ≥5，方向一致性 ≥95%。
- 实测版本、native setup、参数、索引状态、全部计时和原生计划随报告归档。Memgraph 使用装载后的 ANALYZE GRAPH；AGE 使用 VACUUM ANALYZE。HugeGraph 正式补测使用官方原生 clear 清空专用图，保存每次 HTTP 204 及重建后的状态、schema 和索引证据。其他引擎按实际支持的原生准备流程执行，未伪造统一统计刷新命令。

## 证据与复算

[登记清单](../results/detection-20261009/issue_inventory.csv) 从每份报告的实际查询找到唯一正式输入，并重新计算全部 40 组样本、同查询噪声与固定种子 0 的 bootstrap 区间。原始区间也单独保留，不能用最显著的参考比较替代报告中的 A／B 对照。

[注册元数据](../results/detection-20261009/registered/README.md) 保存实验完成时的登记快照。根 bugs.csv 后续维护项目总账和上游状态，部分本地编号已关联为 GitHub issue 编号；原输入指纹不变。内置 Pattern 保留原 input_hash；原生机制根据实际 setup、查询、参数和 schema mapping 生成输入指纹，单列为 native:<mechanism>，不伪装成公共 IR 或 P1–P16 输入。

```bash
python Src/summarize.py --results results/detection-20261009/registered --bugs bugs.csv --output results/detection-20261009/registered/summary
```

实验完成时这些结果登记为人工核查后的本地 reproduced 条目。本次实验操作未向开发者提交报告；用户后续提交的 issue 和登记状态以根 bugs.csv 为准。不同数据库和不同可复现现象分开登记；登记数量不表示已经证明的独立源代码根因数。例如 JanusGraph 与 HugeGraph 的 filter-wrapper 都涉及共同的 TinkerPop 策略次序机制。

本轮含定向机制补测与规模调整，不是相同预算的 Pattern 对比实验。仅能报告本轮具体命中；不能据此估计完整 Pattern 覆盖、边际贡献、发现率或 BUG／小时。

## 排除与验证

低于 100 ms 的候选、缺少额外工作证据的时差、普通 Gremlin 显式遍历顺序差异、准备失败与资源干扰不进入 BUG list。FalkorDB 一次内存终止的轮次标为 EXCLUDED；Memgraph 统计刷新前的结果只作准备记录；Ladybug 的固定链对照只作探索，最终报告采用相同 TRAIL 操作。HugeGraph 一次慢删除未完成确认的轮次排除；n75000 的 COUNT 候选虽有显著差异，却未通过同查询噪声门槛，最终改用另一次完整 n100000 确认，不混合旧样本。

源代码只增加可配置的绝对时间差门槛及 Memgraph 当前统计／计划准备和证据保存。离线测试结果：1185 passed，242 项显式集成测试未选择。真实数据库验证由本轮正式配对、独立重建和原生计划记录提供；不把离线测试数量当成真实数据库实验次数。

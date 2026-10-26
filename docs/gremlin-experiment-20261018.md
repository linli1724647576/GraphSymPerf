# Gremlin 现有 Pattern 扩展实验（2026-10-18）

本轮新增 ArcadeDB 的 Gremlin 语言目标。只使用现有 `GraphSpec`、P 模板、查询中间表示、变换和测量流程；没有新增 Pattern、Native 性能探针或绕过等价性检查。目标选择及维护证据见 [目标调研](gremlin-target-selection-20261018.md)，接口使用见 [Gremlin 实测覆盖](gremlin-support.md)。

## 环境与接口门禁

实机返回 `ArcadeDB/26.10.2 (build e854692736b48971f92dff78a1e727bc570bb6a2/1792085686249/main)`，固定版本依赖 TinkerPop 3.8.2，使用默认 Java Gremlin 引擎与原生 HTTP 接口。运行于 macOS ARM64 的独立 Docker 实例，JVM 设置为 `-Xms1g -Xmx4g`；镜像摘要、服务版本和配置保存在 [environment.json](../results/gremlin-extension-20261018/environment.json)。

在性能测试开始前完成：

- 全部离线回归：**1250 passed，349 integration deselected**。
- 已有语义反例：**29 passed**，覆盖 NULL、布尔与数值类型、整数边界、重复行、平行边、自环、空子查询、CASE、COALESCE 和变量作用域。这次与构造期望值比较，未重跑 Neo4j 对照。
- 已有 Pattern 接口：**75 passed**，包括 12 项 Pattern × 2 个查询种子 × 3 档复杂度，以及 P10/P11 的 3 项既有空输入和多匹配图状态。逐例验证构图、索引、结构约束、A/B/reference 完整结果、原生计划、重新连接、独立重建和归档读回。
- 损坏图拒绝检查：**3 passed**，覆盖逆边配对键错误、逆边缺失和参考边缺失，确认新状态检查能够发现错误图。

门禁证据见 [interface-gate.json](../results/gremlin-extension-20261018/interface-gate.json)、[正向记录](../results/gremlin-extension-20261018/interface/validation-summary.json)和[损坏图记录](../results/gremlin-extension-20261018/interface/negative-state-checks/validation-summary.json)。接口证据目录设置 `EXCLUDED.json`，不进入性能发现或 Pattern 贡献统计。

## 实验边界与判据

本轮范围为 P1、P2、P4、P5、P6、P8、P9、P10、P11、P12、P14、P15。现有 Gremlin 编译器不支持的 P3、P7、P13、P16 和未纳入其覆盖的 S1–S10 不作为阴性实验。

沿用已有报告阈值：配对中位倍率至少 5，95% bootstrap 区间下界至少 5，中位差值严格大于 100 ms，方向一致性至少 0.95，差值超过同查询噪声。每个通过初筛的比较包含两次独立重建，每次 20 组平衡 AB/BA、各 10 组 A/A 和 B/B。报告入选时另行复算每次确认的倍率和差值，要求分别满足 5 倍和 100 ms 门槛。

普通计时包含 HTTP 提交、完整结果接收和解码，排除连接建立、构图和 PROFILE。原生 `GremlinExecutionPlan` 的 `elements`、`traversers` 和 `cost` 用于说明工作量，不冒充普通查询耗时。本轮 30 秒为客户端查询阈值；`arcadedb.command.timeout=30000` 不等于 Gremlin 的独立服务端超时。超时不作为完整计时，取消状态未知时停止目标，并重启专用实例后才继续。

现有执行器把 Gremlin 显式遍历的稳定差异保存为 `timing_observation`。只有证明具体优化规则遗漏了可避免工作，并完成复验与根因去重，才写入 `BUG list`；单纯改变显式遍历起点、方向或顺序导致的工作差异不登记为优化 BUG。源码审查见 [优化规则审查](gremlin-optimizer-audit-20261018.md)。

## 性能结果与报告筛选

**已完成 72 个唯一输入：71 个有效、1 个超时；新增可登记遗漏优化 BUG 为 0。** 12 项 Pattern 各有 6 个输入。基础轮次采用 `complexity=0`、规模 1,000／10,000、查询种子 0／17；补测采用 `complexity=2`、规模 10,000、同一对种子。所有图状态种子均为 0。

| 执行结果 | 输入数 | 判定 |
|---|---:|---|
| `no_candidate` | 47 | 未通过原有初筛，不作无缺陷保证 |
| `not_confirmed` | 17 | 已初筛但未通过全部确认门槛 |
| `timing_observation` | 7 | 耗时门槛通过；逐例计划审查后不登记 BUG |
| `timeout` | 1 | 未完成完整结果检查，排除有效性能输入 |

71 个有效输入保存 **160 份原生详细 PROFILE**。全部 72 份归档已通过输入指纹、IR、查询文本和绑定读回检查；所有已完成确认比较的配对统计、bootstrap 区间与噪声门槛已从原始样本复算，逐项对应原记录。[复算脚本](../results/gremlin-extension-20261018/recount.py)、[复算结果](../results/gremlin-extension-20261018/recomputed-evidence.json)、[分 Pattern 汇总](../results/gremlin-extension-20261018/summary/summary.csv)。

七个超阈值输入如下。表中统一列出 **原始 B/A 的配对中位倍率和 B−A 的配对中位差值**，不把顶层最大参考比较混作 A/B，也不是两个耗时中位数的商。这七项 AB 比较均通过两次确认各自的 5 倍、100 ms 条件及其余门槛。

| Pattern | complexity | scale | seed | B/A 倍率 | B−A（ms） | 证据 |
|---|---:|---:|---:|---:|---:|---|
| P12 | 0 | 1,000 | 17 | 155.29 | 1423.17 | [原始记录](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/0006-P12-n1000-6e979aad/result.json) |
| P4 | 0 | 10,000 | 17 | 95.59 | 146.50 | [原始记录](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0012-P4-n10000-e7e40688/result.json) |
| P5 | 0 | 10,000 | 17 | 117.33 | 162.76 | [原始记录](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/0028-P5-n10000-20d344be/result.json) |
| P4 | 2 | 10,000 | 0 | 249.47 | 597.67 | [原始记录](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0011-P4-n10000-9b5a1881/result.json) |
| P4 | 2 | 10,000 | 17 | 184.47 | 1972.22 | [原始记录](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0012-P4-n10000-a68eedc1/result.json) |
| P5 | 2 | 10,000 | 0 | 254.37 | 1254.01 | [原始记录](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0013-P5-n10000-2b35c271/result.json) |
| P5 | 2 | 10,000 | 17 | 95.24 | 271.48 | [原始记录](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/0014-P5-n10000-19d82fcd/result.json) |

- **P12：**慢侧先产生 1,001 个上游候选，再对每个候选扫描 1,001 个 Low，产生 1,002,001 个中间结果。实际类型扫描替换已生效；相关 `select→values→is` 过滤不具备现有属性索引规则的形状。差异可由原始显式遍历顺序解释。
- **P4／P5：**实际计划显示慢侧先执行昂贵的 COUNT／EXISTS，再读取决定条件；快侧先读取条件，跳过 40,000 个 noise 邻居。逐例检查了深层 AND／OR／CASE／COALESCE，未选分支确实被跳过。部分重复属性读取来自现有编译器为三值逻辑明确生成的 TRUE／FALSE 判定，不能归咎于引擎多执行未选分支。
- 有些 B/reference 比较因同查询噪声大于配对差值未通过；这些比较保持 `not_confirmed`，不借用同一输入 AB 的通过状态。各比较原始慢侧、工作量及源码依据见 [逐案例审查](gremlin-optimizer-audit-20261018.md)。

**P10 有值得保留的低于阈值线索。** `complexity=0, scale=10000, seed=0` 的 A 以索引定位 1 个 Low，B 的后续片段仍用普通 `GraphStep` 扫描 50,003 个顶点；它与 `ArcadeTraversalStrategy` 首次类型扫描替换后提前退出的源码一致。但普通初筛仅为 **1.2735 倍、30.40 ms**，没有进入独立确认，不符合本轮报告门槛。另一个 seed 17 案例也观察到后续类型扫描未替换，但仅约 1.03 倍。不得把计划证据当作已确认的超阈值性能缺陷。

相关控制流曾在正确性问题 [ArcadeDB #9140](https://github.com/ArcadeData/arcadedb/issues/9140) 中被提到，其修复处理的是中途计数吞掉前序输入，不等于修复了本轮观察到的后续图扫描优化机会。因此这里不宣称首次发现该代码，也不冒称开发者确认了本轮性能问题。

### 超时、续跑与重放

首轮 P12 的 `complexity=0, scale=10000, seed=17` 触发客户端 30 秒超时，执行器停止目标，记录 `cancellation_verified=false`。未将它计入有效性能输入，也没有伪造完整耗时或下界倍率。随后重启本轮专用容器，健康检查与版本复核通过后，完成剩余十项 Pattern 的续跑。[重启记录](../results/gremlin-extension-20261018/restart-after-p12-timeout.json)

三个正式运行目录为 [基础首轮](../results/gremlin-extension-20261018/performance/20261018T064439Z-bd454e/run.json)、[安全重启后的续跑](../results/gremlin-extension-20261018/performance-continuation/20261018T064839Z-9ce7e2/run.json)、[复杂度补测](../results/gremlin-extension-20261018/performance-targeted/20261018T065228Z-9a4e63/run.json)。每个 `run.json` 保留实际预算、用时和停止原因；汇总中的 `target_stopped` 包含首轮尚未执行部分的历史状态，不意味着续跑仍未覆盖。案例之间才检查时间预算，不把它描述为强制中断已开始确认的硬上限。

另完成 **2 项真实重放检查**：ArcadeDB 同目标 P10 重放，以及旧 JanusGraph P15 归档按已保存 IR 重新编译到 ArcadeDB。原始文件保持逐字节一致，同目标保留输入指纹，跨目标保留 `source_input_hash`；均完整执行并读回归档。[重放证据](../results/gremlin-extension-20261018/replay-validation.json)。它们使用各自存档协议，只作接口验证，设置 `EXCLUDED.json`，不混入上述 72 个输入。

### BUG list 与复现入口

本轮没有满足“超过阈值且证实遗漏优化”的案例，故不编造或降格新增英文 issue 报告，根 `bugs.csv` 未新增条目。[ArcadeDB BUG 索引](../BUG%20list/ArcadeDB/README.md) 已注明检查结果和未报原因。

当前结果目录的 `bugs.csv` 只保留根总账中与本轮输入指纹匹配的记录，目前为零条；不把不在当前证据目录中的历史 BUG 带入本轮统计。可用以下命令复核，不需要重新测量：

```bash
python results/gremlin-extension-20261018/recount.py
python Src/summarize.py --results results/gremlin-extension-20261018 \
  --bugs results/gremlin-extension-20261018/bugs.csv \
  --output results/gremlin-extension-20261018/summary --language gremlin
```

原始环境、三个运行配置、完整查询、图配方、配对样本和计划均保存在 `results/gremlin-extension-20261018/`。结果目录沿用仓库忽略规则，本轮源码、配置和测试可独立审查；上述有限覆盖不能推出 ArcadeDB 不存在其他遗漏优化。

完成后已停止本轮专用测试容器，保留文件和数据库数据；没有后台性能测试继续运行。最终一致性核对见 [final-validation.json](../results/gremlin-extension-20261018/final-validation.json)。

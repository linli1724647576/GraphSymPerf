# 文献综述与引用审计

> 核查日期：2026-07-22。元数据以 Crossref、作者论文或会议/PVLDB 页面为准；引用不是按题名相似度添加，而是逐项检查它是否支持正文对应主张。

## 1 最接近工作的定位

| 工作 | 测试对象与预言 | 与 GraphSymPerf 的关系 | 本文必须避免的错误表述 |
|---|---|---|---|
| GDsmith \cite{hua2023gdsmith} | Cypher 图与查询生成；差分结果预言 | GraphSymPerf 使用其生成边界 | 不应把 GDsmith 说成性能测试器 |
| GDBMeter \cite{kamm2023gdbmeter} | 图数据库查询分区；逻辑结果预言 | 说明数据库逻辑测试的无标准答案预言 | 不应把查询分区当成本文新规则 |
| Gamera \cite{zhuang2023gamera} | 图结构感知蜕变关系；逻辑问题 | 说明图原生关系的重要性 | 不应声称本文首次为图数据库设计 MR |
| GRev \cite{mang2024grev} | 图数据库等价查询重写；逻辑、性能与异常问题 | 最接近且最强的直接相关工作 | 不得声称首次用等价查询发现图数据库性能问题 |
| GQS \cite{yin2025gqs} | 为期望属性值合成 Cypher 查询；显式 ground truth | 最新图数据库逻辑测试路线，绕开差分/MR 无真值问题 | 不应把所有图数据库测试概括为差分或 MR |
| GraphQE \cite{tang2025graphqe} | 证明有序 bag 语义下的 Cypher 查询等价性 | 可用于压力测试或增强规则证书 | 不应把规则证书写成通用 Cypher 等价证明器 |
| AMOEBA \cite{liu2022amoeba} | 同一关系 DBMS 上的等价 SQL 与时间差 | 与本文共享“同版本等价查询”性能预言 | 不得把该总体思路写成本文首创 |
| APOLLO \cite{jung2019apollo} | 同一 SQL 在新旧 DBMS 版本间的回归 | 支持性能验证、归约和报告链的重要性 | 不应把跨版本回归与本文同版本异常混为一类 |
| Metamorphic Testing \cite{chen2018metamorphic} | 蜕变测试的一般框架 | 支持方法范式定义 | 不能用综述替代图数据库专门工作的比较 |
| Delta Debugging \cite{zeller2002dd} | 最小化失败诱发输入 | 支持归约思想 | 本文仍需说明成对、语义与运行时约束的新增困难 |

## 2 核心新颖性判断

GraphSymPerf 的可辩护定位是：**面向图数据库等价查询性能异常的、带安全证书和独立运行时复验的 issue-grade 证据链**。它不是新的等价查询概念，也不是第一个图数据库性能测试器。

与 GRev 相比，当前可主张的方法差异包括：

1. 规则适用性由显式 `SafetyCertificate` 绑定到 AST 位置、作用域、类型/空值和方言能力，而不是只在生成器结构层面假定等价。
2. 发现样本与验证样本分离；验证在两个新进程和重载图快照中完成。
3. 运行时判定同时要求配对随机化、自身对照、绝对噪声下限和方向一致性。
4. 归约保持查询对和规则见证，并在归约后从零复验。
5. 计划与性能计数器只解释已通过候选，不参与候选真值。

这些差异目前仍主要是设计贡献。只有完成 GRev 共同子集比较和内部消融后，才能声称它们实质降低误报或提高开发者可用性。GraphQE 的出现还意味着“规则带 proof ID”本身不足以构成强贡献；论文需要说明证明义务的内容，并实测 GraphQE 对核心变换的覆盖。

## 3 引用 locator 审计

| citekey | DOI / 主来源 | 正文用途 | locator | 判定 |
|---|---|---|---|---|
| `hua2023gdsmith` | [10.1145/3597926.3598046](https://doi.org/10.1145/3597926.3598046) | 图/数据引导生成；28/23/14 | Abstract；Introduction；paper pp.163–164 | supports |
| `kamm2023gdbmeter` | [10.1145/3597926.3598044](https://doi.org/10.1145/3597926.3598044) | 查询分区用于 GDBMS；40/35/27 | Abstract；§5.1 | supports |
| `zhuang2023gamera` | [10.14778/3636218.3636236](https://doi.org/10.14778/3636218.3636236) | 图感知 MR；39/15/3 | Abstract；p.837 Introduction | supports |
| `mang2024grev` | [10.1145/3597503.3639200](https://doi.org/10.1145/3597503.3639200) | EQR、性能规则、22/15/3、局限 | Abstract；§4.4；Table 2；§6.1 | supports |
| `liu2022amoeba` | [10.1145/3510003.3510093](https://doi.org/10.1145/3510003.3510093) | 等价 SQL 性能预言；39/6/5 | ICSE 2022 final paper Abstract | supports |
| `jung2019apollo` | [10.14778/3357377.3357382](https://doi.org/10.14778/3357377.3357382) | 跨版本性能回归、验证与归约 | Abstract；§1；§4 | supports |
| `chen2018metamorphic` | [10.1145/3143561](https://doi.org/10.1145/3143561) | 蜕变测试一般背景 | Abstract；§1 | supports |
| `zeller2002dd` | [10.1109/32.988498](https://doi.org/10.1109/32.988498) | 失败诱发输入最小化 | Abstract；§2 | supports |
| `yin2025gqs` | [10.1145/3749186](https://doi.org/10.1145/3749186) | 合成查询与显式 ground truth | Abstract；§1 | supports |
| `tang2025graphqe` | [10.1109/ICDE65448.2025.00293](https://doi.org/10.1109/ICDE65448.2025.00293) | Cypher 有序 bag 等价与 SMT 证明 | Abstract；§III；ICDE pp.3931–3944 | supports |

## 4 元数据与版本风险

- 10 条 DOI 的题名、作者、年份和 venue 已核对；APOLLO 还由 PVLDB 官方 PDF核对页码 57–70。
- AMOEBA 的 2021 年预印本与 2022 年 ICSE 正式论文报告数字不同。正文只使用正式 ICSE 版本的 39 potential / 6 confirmed / 5 fixed。
- GRev 已明确重现性能候选 5 次。正文不能将其描述成“只执行一次”的弱基线。
- GQS 的 Crossref 出版年为 2025，官方 SIGMOD 2026 program 将其列为 SIGMOD 2026 论文；参考文献同时保留 PACMMOD 年份和会议呈现信息，避免误写。
- GraphQE 已核对为 ICDE 2025 正式论文，不能继续只引用 arXiv 版本。
- 目标会议尚未确定，因此当前参考文献为可读 Markdown 条目；投稿前需按目标模板生成 `.bib` 并再次执行 100% 引用审计。

## 5 仍缺少的相关工作任务

- `[RESULT GAP]` 在当前数据库版本上复现 GRev，形成共同子集基线。
- `[RESULT GAP]` 使用 GraphQE 检查核心证书实例，报告证明成功、超时和不支持结构。
- `[MATERIAL GAP]` 继续跟踪 2026 年尚处于预印本阶段的图数据库测试工作；未同行评审工作不作为核心新颖性结论的唯一依据。
- `[MATERIAL GAP]` 确定目标会议后补齐其近三年接收论文的写作与评估惯例，但不以接收论文数量替代技术比较。

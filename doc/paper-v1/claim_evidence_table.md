# Claim—Evidence Table

> 核查日期：2026-07-22。所有带数字的主张在第一版中全部核查；尚未完成的实验以 GAP 标记。`verified` 只表示当前来源与文字一致，不表示该结果已经满足正式主实验标准。

| ID | 类型 | 论文主张 | 证据指针 | 状态 | 限制 |
|---|---|---|---|---|---|
| CE-01 | 实验设计 | 正式矩阵包含 7 个系统、每系统 16 个分片、每分片 1,000 对，共 112,000 个接受查询对 | `GraphSymPerf-runtime-first-full/config/experiment.yaml` | verified | 这是设计，不是完成结果 |
| CE-02 | 方法 | 初筛为 3 次预热和 5 组随机配对；独立验证为两个会话、每会话 5 次预热和 20 组平衡配对 | 同上；`docs/validation.md` | verified | 阈值需做敏感性分析 |
| CE-03 | 方法 | 验证门槛为中位倍率 ≥3.0、95% bootstrap CI 下界 ≥2.0、绝对差超过 A/A p99，且两会话方向一致 | `GraphSymPerf-runtime-first-full/config/experiment.yaml`; `docs/validation.md` | verified | 不应写成领域通用标准 |
| CE-04 | 方法 | 正式目标为 Neo4j、Memgraph、FalkorDB、AgensGraph、ArcadeDB、Apache AGE、LadybugDB | `GraphSymPerf-runtime-first-full/config/experiment.yaml` | verified | 公开 issue 当前只覆盖其中六个上游 |
| CE-05 | 当前结果 | 公开问题列表有 29 条，其中性能 25 条、逻辑 4 条 | `bug_list_snapshot.md` | verified | 用户维护快照；跨协议累计值 |
| CE-06 | 当前结果 | 10 条 fixed、5 条 confirmed、1 条 uninterested、13 条 waiting reply | `bug_list_snapshot.md` | verified | 状态语义由人工维护 |
| CE-07 | 当前结果 | fixed + confirmed 共 15 条开发者验证问题 | `bug_list_snapshot.md` | verified | waiting reply 不进入分母 |
| CE-08 | 当前结果 | 2026-07-22 GitHub 状态为 15 closed、14 open | 29 个上游 GitHub issue API 响应；`bug_list_snapshot.md` | verified | open/closed 不等同开发者反馈语义 |
| CE-09 | 试运行 | Neo4j 试运行完成 3/3 分片和 3,000/3,000 个接受查询对 | `runs/neo4j_3x1000_2026_07_13/neo4j_3x1000_2026_07_13-c130fecf4557/run_summary.json`; `analysis/neo4j_3x1000_report.md` | verified | 不是 clean formal run |
| CE-10 | 试运行 | 3,000 对产生 1 个初筛候选、0 个验证通过、0 个可提交问题 | 同上；`analysis/top3_issue_assessment.json`; `validation/candidate_7e50bcc1.json` | verified | 只覆盖一个系统和一个规则 |
| CE-11 | 试运行 | 发现倍率 2.203×；独立复验 1.048× 与 1.077×，绝对差 0.022 ms 与 0.032 ms | `validation/candidate_7e50bcc1.json` | verified | 四舍五入展示；原始值在 JSON 中 |
| CE-12 | 试运行 | 试运行无分片失败、重试、Neo4j 重启或 OOM | `.light/passport.yaml`; `analysis/neo4j_3x1000_report.md` | verified | 不代表所有目标的可靠性 |
| CE-13 | 相关工作 | GDsmith 报告 28 个问题，其中 23 个被确认、14 个已修复 | DOI 10.1145/3597926.3598046，摘要及引言 | verified | 仅用于相关工作背景 |
| CE-14 | 相关工作 | GDBMeter 报告 40 个独立未知问题，其中 35 个被确认、27 个已修复 | DOI 10.1145/3597926.3598044，摘要 | verified | 包含逻辑、错误与崩溃问题 |
| CE-15 | 相关工作 | Gamera 报告 39 个问题，其中 15 个被确认、3 个已修复 | DOI 10.14778/3636218.3636236，摘要及 p.837 | verified | 主要针对逻辑问题 |
| CE-16 | 相关工作 | GRev 报告 22 个问题、15 个被确认，其中 3 个为性能问题 | DOI 10.1145/3597503.3639200，摘要及 Table 2 | verified | 最接近工作，必须直接比较 |
| CE-17 | 相关工作 | GRev 的性能筛选要求至少 5× 且至少 1,000 ms，并对性能候选重现 5 次 | 同上，§4.4 与 §6.1 | verified | 不能把该协议写成简单单次计时 |
| CE-18 | 相关工作 | AMOEBA 最终 ICSE 版本报告 39 个潜在性能问题、6 个被确认、5 个已修复 | DOI 10.1145/3510003.3510093，摘要 | verified | 预印本数字不同，正文使用正式版 |
| CE-19 | 相关工作 | GQS 使用已知期望属性值合成 Cypher 查询，报告 36 个未知问题，其中 26 个为逻辑问题 | DOI 10.1145/3749186，摘要 | verified | 主要用于逻辑问题，不是性能基线 |
| CE-20 | 相关工作 | GraphQE 是 ICDE 2025 的 Cypher 查询等价证明器，评估数据包含 148 对并证明其中 138 对 | DOI 10.1109/ICDE65448.2025.00293，摘要及实验 | verified | 正文第一版只引用其能力，不把数字作为本文结果 |
| CE-21 | 结果缺口 | 正式 112,000 对跨系统结果、GRev 基线、GraphQE 覆盖、消融、归约统计尚未完成 | 当前仓库与运行产物盘点 | GAP | 未完成前不得填预测值 |

## 诚信门结论

- 第一版所有数值主张已逐项核对来源。
- 正文明确区分“正式设计”“跨版本累计问题集”和“单系统试运行”。
- CE-21 是阻止当前草稿成为投稿稿的关键缺口。

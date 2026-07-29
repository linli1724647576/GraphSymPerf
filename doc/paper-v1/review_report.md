# 第一版严审与写作闸门报告

## 结论

**有条件推进，不可投稿。** 方法证据链清楚，公开问题数量足以说明研究对象具有实际价值；但最接近工作 GRev 已覆盖“图数据库 + 等价查询 + 性能问题”，而正式 112,000 对矩阵、直接基线、消融和归约结果尚未完成。当前版本适合作为全篇写作骨架，不足以支持 CCF-A 类会议的有效性与新颖性要求。

## 1 Idea 严审

| 维度 | 当前判断 | 主要依据 | 必改项 |
|---|---|---|---|
| 新颖性 | 中等 | GRev 已提出图数据库等价查询重写与性能检测 | 把贡献锁定为安全证书和 issue-grade 运行时证据链；完成直接比较 |
| 重要性 | 较高 | 29 条公开报告中有 15 条 fixed/confirmed | 区分累计案例与正式实验，不夸大开发者确认率 |
| 方法严谨性 | 较高 | 独立会话、噪声对照、归约后复验、fail-closed 设计 | 用消融证明每个门禁的实际作用 |
| 实验证据 | 不足 | 只有跨版本 issue 集和单系统 3,000 对阴性试运行 | 完成正式矩阵和基线 |
| 可复现性 | 较高 | 版本、哈希、图指纹、状态产物设计完整 | 清理 GDsmith dirty provenance；保存每对紧凑计时摘要 |
| 通用性 | 待证 | 协议计划覆盖 7 个系统，公开问题覆盖 6 个 | 补齐 7 系统正式结果和方言拒绝分析 |
| 开发者价值 | 初步成立 | 10 fixed、5 confirmed | 冻结观察窗口并保存维护者证据 URL |
| 论文成熟度 | 初稿 | 结构完整但结果节大量 GAP | GAP 清零后再做投稿级评审 |

### 三个可能直接导致拒稿的问题

1. **贡献与 GRev 重叠。** 若标题、摘要或引言声称首次进行图数据库等价查询性能测试，事实不成立。
2. **主实验尚未完成。** 29 条累计 issue 不能替代固定预算矩阵，3,000 对试运行也不能支持跨系统有效性。
3. **缺少公平基线与消融。** 当前无法判断严格证据链是否只增加成本，还是确实降低误报并改善报告质量。

另一个需要正面处理的技术压力来自 GraphQE：ICDE 2025 已提供 Cypher 等价性证明器。GraphSymPerf 必须阐明规则证书与通用等价证明的关系，最好实测 GraphQE 对核心规则实例的覆盖，而不是只把 `proof_id` 当作形式化保证。

## 2 术语与口径一致性

| 统一术语 | 定义 | 禁止替换成 |
|---|---|---|
| accepted query pair | 通过安全、执行、完整 typed equivalence 与去重的查询对 | bug、candidate |
| runtime candidate | 仅通过 5-pair 初筛的接受查询对 | confirmed issue |
| VALIDATION_PASSED | 两个独立会话均过门禁的原始查询对 | submitted issue |
| REDUCED_VALID | 归约后重新通过完整验证 | partial reduction |
| SubmitReadyIssue | 通过证书、归约验证与去重的可提交对象 | public report |
| Reported Issue Report | 已有上游 URL 的提交报告 | developer-validated bug |
| Developer-Validated Bug | developer status 为 fixed 或 confirmed | closed issue |
| performance anomaly | 经协议识别的稳定运行时不对称 | optimizer bug（除非有根因证据） |

数据库命名统一为 `Neo4j`、`Memgraph`、`FalkorDB`、`AgensGraph`、`ArcadeDB`、`Apache AGE` 和 `LadybugDB`。英文正文第一次出现写全称 `Graph Database Management System (GDBMS)`；后续使用 GDBMS。

## 3 伦理与科研诚信检查

- 不涉及人类受试者、动物或个人敏感数据；图数据为合成数据。
- 只对授权测试实例运行，不面向生产服务施压。
- 公开 issue 提交属于开发者协作，不应披露无法最小化的敏感生产数据。
- 阴性结果必须保留。Neo4j 3,000 对试运行的 0 个验证通过候选已如实写入。
- “发现问题—根因假设—归因验证”分开。没有维护者或受控实验支持时，不把执行计划猜测写成根因事实。
- 29 条 issue 的人工状态与 GitHub open/closed 分开；waiting reply 不被计为误报或确认。
- 生成式 AI 的使用范围已在正文披露，投稿前按目标会议最新政策调整。

## 4 初稿自检

| 检查项 | 状态 | 说明 |
|---|---|---|
| 背景—问题—gap—方法—贡献闭环 | PASS | 引言已明确 GRev 是最近工作 |
| 研究问题可由实验回答 | PASS | RQ1–RQ5 均绑定分母和产物 |
| 数字均有来源 | PASS | 见 `claim_evidence_table.md` |
| 引用真实且支撑对应句 | PASS | 8 条核心引用已做 locator 审计 |
| 结果无虚构 | PASS | 未完成项保留 GAP |
| 相关工作定位诚实 | PASS | 删除“首次”等不可支持表述 |
| 实验足够支持结论 | FAIL | 正式矩阵、GRev 基线和消融未完成 |
| 局限是否具体 | PASS | 明确累计 issue、单系统试运行和代理边界 |
| 必备声明是否齐全 | WARN | 已留 Data/Code/COI/Funding/CRediT/AI 占位，待作者和 venue 补齐 |
| 语言是否避免空泛和营销 | PASS | 贡献使用可核动作，不使用“革命性”“全面领先”等措辞 |

## 5 下一轮最小修改顺序

1. 冻结目标会议和篇幅，决定正文与附录预算。
2. 完成每 DBMS 的 1×10、1×100、1×1,000 门禁，修复 provenance 和产物保留缺口。
3. 运行正式 112,000 接受对矩阵并生成 RQ1–RQ3 表。
4. 对所有 validation-passed 候选完成成对归约与归约后复验，回答 RQ4。
5. 在共同目标上复现 GRev，使用 GraphQE 检查核心规则实例，并完成验证/噪声/证书/归约消融。
6. 冻结 issue 观察窗口，重新核对上游状态，回答 RQ5。
7. 用实际结果重写摘要、贡献、结果和结论，清零全部 GAP。

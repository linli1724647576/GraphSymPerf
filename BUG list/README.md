# 开发者报告与复现案例

按 P1–P16 整理，每个 Pattern 两个案例，共 32 个独立附件包。报告正文沿用已提交 Issue 的英文格式。

- **可报告：10 份本地独立复验的性能候选报告**，涉及 P3、P4、P10、P11、P13；等待开发者评估，不能直接计为 10 个独立 BUG。
- **待验证：20 份案例记录**，其中 19 份本轮未达到初筛门槛，P5 的 Memgraph 案例出现 2.35 倍差异但未达到 3 倍确认门槛。它们不能作为已发现缺陷提交。
- **预期行为：2 份 P16 观察记录**，单列失败元素位置与列表构造差异，排除 BUG 认定。

每个案例目录包含报告、可直接执行的后端原生构图脚本、两条完整查询、预期结果、原始测量、实际 PROFILE、环境信息及独立 reproduce.py；脚本不依赖 GraphSymPerf 源代码。先在对应版本的空白专用数据库中执行，连接凭据由自己设置。

P3 两例与 P4 两例分别关联同一候选机制，适合作为同一 Issue 的两个复现输入。P4 的第二例是 COUNT>0 输入，但实际被改写为 SemiApply；不宣称聚合缺陷。P11 两例分别测试八匹配重复遍历与空低侧提前终止。P10/P13 的两例来自不同数据库。

以下为复现覆盖索引，不是 BUG 数量表。先前发现轮的 40 条候选统计保持不变；本轮补证据与 COUNT 新例另行记录。

| Pattern | 案例 | 数据库 | 状态 | 独立比较 | 报告 |
|---|---:|---|---|---|---|
| P1 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P1/CASE01/CASE.md) |
| P1 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P1/CASE02/CASE.md) |
| P2 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P2/CASE01/CASE.md) |
| P2 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P2/CASE02/CASE.md) |
| P3 | 1 | neo4j | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Neo4j/P3/CASE01/ISSUE.md) |
| P3 | 2 | neo4j | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Neo4j/P3/CASE02/ISSUE.md) |
| P4 | 1 | neo4j | 可报告候选 | reference / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Neo4j/P4/CASE01/ISSUE.md) |
| P4 | 2 | neo4j | 可报告候选 | reference / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Neo4j/P4/CASE02/ISSUE.md) |
| P5 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P5/CASE01/CASE.md) |
| P5 | 2 | memgraph | 需进一步评估 | symmetry / not_confirmed | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P5/CASE02/CASE.md) |
| P6 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P6/CASE01/CASE.md) |
| P6 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P6/CASE02/CASE.md) |
| P7 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P7/CASE01/CASE.md) |
| P7 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P7/CASE02/CASE.md) |
| P8 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P8/CASE01/CASE.md) |
| P8 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P8/CASE02/CASE.md) |
| P9 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P9/CASE01/CASE.md) |
| P9 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P9/CASE02/CASE.md) |
| P10 | 1 | memgraph | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Memgraph/P10/CASE01/ISSUE.md) |
| P10 | 2 | falkordb | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/FalkorDB/P10/CASE02/ISSUE.md) |
| P11 | 1 | memgraph | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Memgraph/P11/CASE01/ISSUE.md) |
| P11 | 2 | memgraph | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Memgraph/P11/CASE02/ISSUE.md) |
| P12 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P12/CASE01/CASE.md) |
| P12 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P12/CASE02/CASE.md) |
| P13 | 1 | memgraph | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/Memgraph/P13/CASE01/ISSUE.md) |
| P13 | 2 | falkordb | 可报告候选 | symmetry / candidate | [打开](%E5%8F%AF%E6%8A%A5%E5%91%8A/FalkorDB/P13/CASE02/ISSUE.md) |
| P14 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P14/CASE01/CASE.md) |
| P14 | 2 | memgraph | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Memgraph/P14/CASE02/CASE.md) |
| P15 | 1 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P15/CASE01/CASE.md) |
| P15 | 2 | neo4j | 本轮阴性 | symmetry / no_candidate | [打开](%E5%BE%85%E9%AA%8C%E8%AF%81/Neo4j/P15/CASE02/CASE.md) |
| P16 | 1 | neo4j | 预期求值差异 | symmetry / not_confirmed | [打开](%E9%A2%84%E6%9C%9F%E8%A1%8C%E4%B8%BA/Neo4j/P16/CASE01/OBSERVATION.md) |
| P16 | 2 | falkordb | 预期求值差异 | symmetry / candidate | [打开](%E9%A2%84%E6%9C%9F%E8%A1%8C%E4%B8%BA/FalkorDB/P16/CASE02/OBSERVATION.md) |

所有报告与附件均保存在当前工作区。发布状态与 issue_url 保持空值；人工 BUG 贡献表未因生成报告而自动增加。

可报告目录中的每个 CASE 另附 `case-package.zip`，包含该报告的全部复现附件，便于随 Issue 一起上传。

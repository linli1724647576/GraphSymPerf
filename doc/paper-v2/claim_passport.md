# Claim Passport · GraphSymPerf v2

> 初稿门：所有引用型和数值型主张均登记。`✅已验证` 表示当前来源与文字一致；`⏳待核` 和 `🚫GAP` 在投稿前必须清零或从正文删除。

| claim-ID | 类型 | claim | 来源指针 | 状态 | 核查日期 | 备注 |
|---|---|---|---|---|---|---|
| C-01 | ① | GRev 使用 ASG 和 RWC 生成等价图查询 | GRev official PDF; DOI 10.1145/3597503.3639200 | ✅已验证 | 2026-07-27 | 原论文 §3 |
| C-02 | ① | Dinkel 在生成过程中维护 query context 和 graph schema | PVLDB official PDF; DOI 10.14778/3797919.3797928 | ✅已验证 | 2026-07-27 | 原论文 §3.1 |
| C-03 | ① | Dinkel 包含表达式级双重否定和布尔中性变换 | PVLDB official PDF Table 3 | ✅已验证 | 2026-07-27 | 不得把这些规则本身写成本文创新 |
| C-04 | ① | GraphQE 研究 Cypher 查询等价证明 | DOI 10.1109/ICDE65448.2025.00293；v1 citation audit | ✅已验证 | 2026-07-22 | 终稿前再次核 locator |
| C-05 | ③ | 当前代码具有 deliberately small/provable Cypher AST fragment | `src/graph_sym_perf/cypher/ast.py`; `parser.py` | ✅已验证 | 2026-07-27 | 只支持有限片段 |
| C-06 | ③ | 当前代码记录 `SemanticProof` 并比较规范化 RA | `src/graph_sym_perf/cypher/proof.py` | ✅已验证 | 2026-07-27 | 不能等同完整 AQCG |
| C-07 | ③ | 当前 `safety_points.py` 仍使用字符串分割，作用域为空且空值性 unknown | `src/graph_sym_perf/transforms/safety_points.py:21` | ✅已验证 | 2026-07-27 | 是 AQCG 重构的直接缺口 |
| C-08 | ③ | 当前转换层和验证层存在两个 `SafetyCertificate` 定义 | `transforms/safety_points.py`; `validation/models.py` | ✅已验证 | 2026-07-27 | 投稿实现前统一 |
| C-09 | ③ | 完整 AQCG、统一三值求值器和 RAE 尚未实现 | 当前源码搜索与代码审计 | 🚫GAP | 2026-07-27 | 正文已标 `[MATERIAL GAP]` |
| C-10 | ② | 累计公开 issue reports 为29个，其中25性能、4逻辑 | 用户列表；`doc/paper-v1/bug_list_snapshot.md` | ✅已验证 | 2026-07-27 | 两个来源的总数和类型一致 |
| C-11 | ② | 当前 confirmed/fixed 工作值为21个 | 用户 2026-07-27 提供的状态列表 | ⏳待核 | 2026-07-27 | 上游逐项复核前不能作为冻结结果 |
| C-12 | ② | 21个工作值由4逻辑和17性能组成 | C-10 与用户状态列表重算 | ⏳待核 | 2026-07-27 | 依赖 C-11 |
| C-13 | ② | v1 的 2026-07-22 快照记录 confirmed/fixed 为15个 | `doc/paper-v1/bug_list_snapshot.md` | ✅已验证 | 2026-07-27 | 与 C-11 冲突，正文已披露 |
| C-14 | ③ | 严格结果等价比较类型、重复次数及语义建立的顺序 | 本轮确认的方法定义 | ✅已确认设计 | 2026-07-27 | 尚需实现和测试 |
| C-15 | ③ | CORE 使用共享语义规则和方言适配器，不包含 DBMS 名称分支 | 本轮确认的方法设计 | ✅已确认设计 | 2026-07-27 | 实现完成后转代码证据 |
| C-16 | ③ | CORE-v1 优先完成8条规则 | 本轮确认的规则组合 | ✅已确认设计 | 2026-07-27 | 最终数量由完成度决定 |
| C-17 | ③ | RAE 可解释性定义为忠实、可追溯和可重放 | 本轮确认的方法定义 | ✅已确认设计 | 2026-07-27 | 不主张主观可读性 |
| C-18 | ③ | 静态覆盖仅在 SAFE 实例内，由证明义务因素驱动 | 本轮确认的方法定义 | ✅已确认设计 | 2026-07-27 | 运行反馈不得进入 |
| C-19 | ② | 性能阈值 \(\tau,\delta\) 与重复配置 | `[RESULT GAP: independent pilot]` | 🚫GAP | — | 不得根据29个 issue 调参 |
| C-20 | ② | AQCG 相对 AST-local 的增益 | `[RESULT GAP: RQ2 ablation]` | 🚫GAP | — | 核心创新成立的关键证据 |
| C-21 | ② | 冻结框架对29个 issue 的 DIRECT/RECONSTRUCTABLE 覆盖 | `[RESULT GAP: replay audit]` | 🚫GAP | — | INSPIRATIONAL 不计覆盖 |
| C-22 | ② | 新方法跨系统逻辑/性能异常产出 | `[RESULT GAP: formal campaign]` | 🚫GAP | — | 不填预测数字 |

## 初稿诚信门小结

- 引用型 claim：已核 GRev 与 Dinkel 主来源；其他引用继承 v1 审计，终稿前需再次全查。
- 数值型 claim：29/25/4 已核；21/4/17 仅为当前工作值，已显式标待核。
- 实验结果：所有未完成内容均使用 `[RESULT GAP]`，未填预测值。
- 实现状态：AQCG、RAE 和静态覆盖均未伪装成当前已完成实现。


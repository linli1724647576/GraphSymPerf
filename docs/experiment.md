# 实验协议

## 固定矩阵

正式实验使用 schema-v2 `config/experiment.yaml`：Neo4j、Memgraph、FalkorDB、AgensGraph、Apache AGE、ArcadeDB、LadybugDB；每系统 16 shards，每 shard 1,000 个 accepted query pairs。每个 shard 有唯一 seed，跨 shard 按 source AST hash 去重。

accepted query pair 必须依次通过：GDsmith read-query 解析、目标方言能力检查、CORE SafetyCertificate、两侧普通执行成功、结果未截断、完整 typed equivalence、运行级 AST 去重。生成了 1,000 条文本不等于接受了 1,000 对。

## Shard 预算

- accepted target：1,000；
- 最多 generated seeds：10,000；
- 最长 shard 时间：6 小时；
- 基础设施瞬态失败最多自动恢复：3 次；绑定 query hash 的确定性 timeout 独立隔离，统一受 6 小时总预算约束；
- 完整 shard artifact：不超过 500 MiB。

达到 seed 或时间边界但 accepted count 不足时写 `INCOMPLETE`。首个 attempt 写入的 `runtime_budget.json` 对 retry/resume 不可重置；attempt 编号不等于基础设施失败次数。超时、崩溃和重启有独立错误分类；不允许无 checkpoint 自动退出，也不允许把 999 补写成 1,000。

## 递进门禁

正式 7 × 16 × 1,000 之前按顺序执行：纯离线测试、故障注入、每 DBMS 1×10、1×100、1×1,000。每一级都核对 accepted rate、typed-equivalence reject、rule quarantine、A/A noise、validation rejection、reducer 结果、恢复能力与 artifact 体积。重构提交不自动启动长跑。

## 统计口径

discovery 使用 5 个随机化 paired executions，只负责宽松筛选。论文 effectiveness 的分母是 accepted query pairs；precision 应报告 runtime candidates 经原始 validation、reducer、reduced validation 后的 survival funnel。不得把历史累计 11 个 issue 混入某次新运行的 yield 分子。

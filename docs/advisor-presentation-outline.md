# 导师汇报大纲

1. 研究问题：语义等价图查询为何出现稳定 runtime 差异，以及旧证据为何容易误报。
2. 核心贡献：safety-point BOOL/SYM 规则空间与 runtime-first oracle。
3. 规则可信性：CORE/EXPERIMENTAL/CONTROL/REMOVED、三值逻辑证明、方言能力与动态 quarantine。
4. 发现流程：GDsmith → accepted pair → 5-pair screen；强调 candidate 不是 issue。
5. 复核流程：两个 fresh session、完整 typed equivalence、A/A/B/B、40 个合并 paired samples。
6. Reducer：paired dependency graph、RuleWitness、局部 1-minimal、reduced validation。
7. 稳定运行：process group deadline、heartbeat、checkpoint、resume、ReliabilityFinding 与 500 MiB budget。
8. 七库兼容：Apache AGE/AgensGraph 方言边界、LadybugDB proxy、无损 REJECT/top-up 策略。
9. 实验设计：7 × 16 × 1,000、递进 gate、RQ1–RQ5 与版本化分母。
10. Issue 证据：SubmitReadyIssue、去重、Reported 与 Developer-Validated 两套数字、90 天右删失。
11. 解释层：plan/PROFILE/PMC 仅用于 reduced issue 的 root-cause hypothesis，不参与筛选或 promotion。
12. 当前风险与下一步：真实 DBMS doctor、1×10/100/1,000 gate、规则特定 reducer witness 与 v3 历史复核。

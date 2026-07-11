# 术语

- **accepted query pair**：通过方言、安全证书、两侧执行、未截断完整 typed equivalence 与去重的查询对；尚未证明性能异常。
- **runtime candidate**：5-pair discovery screen 达到候选阈值的 accepted pair；不能提交。
- **VALIDATION_PASSED**：原始 pair 在两个独立 session 中通过完整等价性、负对照、runtime 与环境门禁。
- **REDUCED_VALID**：paired AST reducer 得到局部 1-minimal 结果，且 reduced pair 再次通过完整独立 validation。
- **SubmitReadyIssue**：由 `promote()` 构造的 typed object；同时满足原始/归约 validation、CORE certificate、方向与去重门禁。
- **Reported Issue Report**：已有可核查上游 URL、stage 为 SUBMITTED 的报告。
- **Developer-Validated Bug**：维护者状态为 confirmed 或 fixed 的已报告案例。
- **ReliabilityFinding**：hang、crash、OOM、连接断开、不可取消 timeout 等可靠性发现；不计 runtime issue。
- **ExplanationEvidence**：reduced validation 后按需采集的 plan、PROFILE、PMC 等解释材料；不改变 stage。
- **discovery ratio**：筛选样本的历史比值，存在选择偏差。
- **validated ratio**：两个独立 session 的保守合并比值，开发者侧只报告该值。
- **INCOMPLETE shard**：达到 seed/time/budget 边界但 accepted target 或阶段证据未完成的 shard；不进入 completed 分母。

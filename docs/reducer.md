# Paired AST Reducer

Reducer 只接收完整的 `VALIDATION_PASSED` 根。输入绑定原始 CandidateBundle、validation report、SafetyCertificate、RuleWitness、graph snapshot、validation policy 和三个固定 gate 协议。

## Gate 顺序

每个成对删除提案按固定顺序检查：

1. 两侧可解析，scope、binding role、输出 schema 与参数保持；
2. RuleWitness 指向的语义关系仍存在；
3. 普通执行得到完整 typed equivalence；
4. 5-pair ordinary runtime screen 仍保持原慢侧与严格门槛。

前一 gate 失败时不运行后一 gate。缓存键绑定 query pair 与 validation-policy hash；plan、PROFILE、PMC 不进入症状谓词。

## 终态

- `REDUCED_VALID`：局部 one-deletion universe 已穷尽、minimality proof hash chain 完整，并通过新的两个独立 validation sessions；
- `REDUCTION_INVALIDATED`：等价或 runtime 症状被确定性破坏；
- `REDUCTION_PARTIAL`：预算耗尽、证据不完整、基础设施错误或未达到局部 1-minimal；只能人工研究，不能自动 export。

生产 witness 当前保守绑定 SafetyCertificate 所在 clause，优先避免删掉真正的等价关系；这可能造成归约不足，但不会通过弱 witness 放大误报。后续优化应增加规则特定 subtree witness，而不是放宽 promotion gate。

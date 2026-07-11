# BOOL/SYM 规则

`config/rules.yaml` 是可执行目录；代码中的 `RuleRegistry` 必须与其 hash 一致。每条规则有版本、tier、proof ID、purity 前置条件、方言能力、site class、role 和 submit eligibility。

## Tier

- `CORE`：证明与动态完整等价检查均通过，可进入正式实验和自动 promotion；
- `EXPERIMENTAL`：用于 pilot，不自动提交；
- `CONTROL`：噪声/解析控制，不生成 issue；
- `REMOVED`：保留裁决身份，不能被 scheduler 选中。

CORE BOOL 为 constant-true disjunction、constant-false conjunction、null partition tautology、null contradiction、null-complete excluded middle。CORE SYM 为 pattern order、predicate order、node order、label order；后者受目标多标签能力约束。

double negation、De Morgan、numeric equality range 等仍是 EXPERIMENTAL。duplicate predicate、edge notation、alpha renaming 是 CONTROL。重复模板、预期重复昂贵求值、未证明 multiplicity/scope 的变换保持 REMOVED 或 EXPERIMENTAL。

## SafetyCertificate 与调度

安全点枚举只覆盖解析器能证明的 MATCH/WHERE fragment。证书绑定 rule、AST path、scope variables、type/nullability、clause context、preconditions、proof ID 和 capability hash。任何动态 typed-equivalence mismatch 立即 quarantine 该 DBMS/rule/version，并保存 counterexample。

scheduler 只读取 accepted count 与 completed validation yield；设置每规则 exploration floor，不能读取解释证据、开发者状态或历史公开结果。增加规则的标准是更强的可证明语义空间与独立 runtime yield，而不是单纯增加规则数量。

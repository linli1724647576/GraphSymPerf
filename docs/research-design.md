# 研究设计

## 问题定义

给定目标 GDBMS、固定 graph snapshot 和由 SafetyCertificate 证明适用的变换关系，生成查询对 `(Q, Q')`。两者必须返回完整、typed-equivalent 结果；若 ordinary execute runtime 在独立复核后仍存在显著、稳定、同方向差异，则形成性能异常候选。发现问题、提出 root-cause hypothesis、验证归因是三个阶段，不能在生成时预设具体优化器缺陷。

## 方法主线

1. GDsmith 生成可回放图与 read query；
2. 根据目标能力枚举 safety point 并应用审计后的 CORE BOOL/SYM rule；
3. 在目标 DBMS 上执行两侧并建立 accepted query pair；
4. randomized paired ordinary execution 做宽松 runtime screen；
5. 两个 fresh session 复核结果、噪声、环境与 runtime；
6. dependency-aware paired AST reducer 最小化无关结构；
7. reduced pair 执行同等强度复核；
8. 运行内去重，构造 SubmitReadyIssue；
9. 按需采集解释材料并形成开发者 reproducer。

## 误报防线

typed sequence/multiset 语义保留类型、NULL/missing、重复数与 Decimal 精确数值；无序 LIMIT/SKIP、volatile/current-time/internal-ID、结果截断、graph fingerprint 漂移和环境 provenance 缺失均 fail closed。discovery 与 validation 分样本，A/A 与 B/B 约束噪声，reducer 不能只保留“看起来更慢”而破坏规则 witness。

## 明确不做

不使用第二生成器，不用查询计划引导规则采样，不把 profile elapsed 当 runtime，不把 crash/hang 当性能 issue，不以 maintainer outcome 反向训练语义 scheduler，也不自动提交 partial reduction。

# 架构

## 主链

```text
CLI
  → ExperimentCommands
  → doctor + immutable RunPlan
  → ShardSupervisor
  → FormalRuntimeFirstShardWorker
  → GDsmith generation + dialect decision
  → accepted query pair + runtime screen
  → ProductionCandidatePipeline
  → independent validation
  → paired AST reducer + reduced validation
  → durable duplicate registry
  → SubmitReadyIssue + ArtifactChainWriter
```

普通用户只接触 `experiment run` 与 `experiment resume`。CLI 不直接构造 worker、validator、reducer 或 exporter；`experiment.commands` 与 `issues.commands` 是稳定公共门面。

## 分层职责

| 层 | 责任 | 禁止事项 |
| --- | --- | --- |
| `generation` / `gdsmith` | 固定 GDsmith 输入、解析、去重和原始产物预算 | 引入第二生成器 |
| `dialects` / `adapters` | 能力判断、无损渲染、连接、图加载与普通执行 | 用 regex 猜测有损兼容 |
| `transforms` / `workload` | 安全点、SafetyCertificate、accepted pair 计数 | 用开发者反馈选择语义 |
| `runtime` / `equivalence` | 配对计时、typed complete-result equality、统计门禁 | 调用 explain/profile |
| `validation` | 两个 fresh session 与 promotion 类型约束 | 复用 discovery 样本 |
| `reducer` | 成对、依赖感知、症状保持的局部 1-minimal 化 | 单边删改或以解释证据保留症状 |
| `artifacts` / `issues` | 内容寻址、hash chain、去重、typed export | 从 raw dict 构造可提交 issue |
| `experiment` | shard 生命周期、心跳、恢复、超时与预算 | 依赖 CLI 私有函数 |

叶子层不反向依赖 CLI 或实验编排。adapter 是边界能力，由 worker/validation 工厂注入；runtime 和 reducer 面向 `execute` 协议。

## 完成态不变量

`ShardSupervisor` 只在以下条件同时满足时写 `COMPLETED`：accepted hash 与 canonical JSONL 一致；数量等于 manifest 目标；screening、validation、reduction 三类证据存在且 hash 可复核；状态链尾部为 `VALIDATING → REDUCING → COMPLETED`。重试耗尽只终止当前 shard。

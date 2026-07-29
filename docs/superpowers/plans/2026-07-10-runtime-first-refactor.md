# GraphSymPerf Runtime-First Refactoring Implementation Plan v3（最终版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** 把 GraphSymPerf 重构成“GDsmith 唯一生成 → 方言能力检查 → SafetyCertificate 约束的等价变换 → 完整结果等价性硬门槛 → 普通执行 runtime 筛选 → 两个独立 validation session → paired AST reducer → reduced-pair 再验证 → plan/PROFILE/PMC 被动解释”的单一实验主线；用一个入口稳定运行 7 个 GDBMS × 16 shards × 每 shard 1000 个 accepted query pairs，并累计形成 30+ 个经过验证、缩减、去重的可提交 issue reports。

**Architecture：** 以 `AcceptedPairBuilder`、`RuntimeHarness`、`IssueValidator`、`CandidateReducer`、`IssueRegistry` 和 `IssueExporter` 六个深模块承载有效查询对构建、测量、复核、缩减、版本归因和发布门禁。GDsmith 是唯一生成器；规则调度采用“每条 CORE 规则的最低探索配额 + 基于已完成 validation 的 runtime yield 分配剩余预算”，但严禁读取 query plan、PROFILE、PMC 或维护者状态。plan、PROFILE 和 PMC 只在 reduced pair 已通过独立 runtime validation 后采集，用于解释而非筛选。

**Tech Stack：** Python 3.11、pytest、现有 GDBMS drivers、GDsmith Java subprocess、版本化 YAML/JSON artifacts、Cypher AST/relational proof、Bootstrap confidence interval、进程组级 hard timeout。

## Global Constraints

- 生成器只有 GDsmith；删除 LDBC dataset、query generator、CLI、配置和测试专用名词，adapter 图加载迁移为中立 `GraphCorpus`。
- 每个 completed shard 必须包含恰好 1000 个 accepted query pairs；“accepted”同时要求 base/transformed 可执行、完整结果语义等价、方言兼容且测量期间无基础设施错误。
- 每个 shard 最多尝试 10,000 个 GDsmith seeds 或运行 6 小时；先触发任一上限且 accepted pairs 不足 1000 时标记 `INCOMPLETE`，不得无限重试或伪装 completed。
- 查询计划不参与生成、规则调度、候选判定、聚类排序或 issue promotion；删除 QPG/plan-guided feedback。
- plan、PROFILE、db hits、rows、memory 等只允许形成 `ExplanationEvidence`。
- issue draft/export 只能消费 `SubmitReadyIssue`，不能直接消费 candidate、profile-supported 或 discovery artifact。
- validation 和 reducer 只使用普通 `execute` runtime 作为症状判据，严禁使用 PROFILE elapsed time。
- SubmitReady 的 runtime 硬门槛为：两个独立 session 均同方向复现、合并 median ratio ≥3.0、95% bootstrap CI 下界 ≥2.0、median absolute delta 大于目标 DBMS 的 A/A noise p99。
- 所有非确定性查询、结果在 A/A 中不稳定的查询、无确定性 `ORDER BY` 的 `LIMIT/SKIP`、依赖未定义顺序的聚合结果、写查询和内部 ID 不稳定查询均不得进入自动 issue 链。
- crash/hang 可以形成独立 `ReliabilityFinding`，但不得计入 30+ runtime issue reports。
- 当前用户未提交修改必须保留；执行清理前必须生成 dry-run 清单并获得用户确认。
- 现有 11 个公开 issue 必须分别迁移为最小证据包；除这些证据包外的旧 `runs/`、`logs/`、PMC/PROFILE、shard 和 GDsmith raw 产物在批准的清理阶段全部删除。
- 冷缓存与热缓存分别建模；正式 issue 默认只使用可复现的 warm-cache protocol。
- 不要求冻结整个 bug-finding campaign；方法、规则和 validator 可以演进，但每个 run 的 resolved manifest 不可变，每个 issue 必须记录 detector/rule/validator/reducer/DBMS 版本，旧 issue 不得追溯归因给新版本。
- 30+ 是项目累计 `Reported Issue Reports` 目标，不是单次运行的停止条件；confirmed/fixed 作为开发者侧外部有效性证据，不能反向决定候选 promotion。
- 所有 GDBMS 执行相同 16×1000 预算，但不要求每个系统都产出 issue；不得为了系统配额降低验证或缩减门槛。

## 最终决策锁定（15 项 grilling 结论）

1. **成功口径：** 在固定单次运行预算和统一反馈快照口径下累计形成 30+ 个经过独立 validation、paired reducer 和去重的可提交 issue reports；confirmed/fixed 不作为停止条件。
2. **计数对象：** 论文主规模指标使用 `Reported Issue Reports`；同时报告独立 bug family 数，但不要求达到 30 个 family。
3. **跨系统要求：** 7 个 GDBMS 全部执行同一实验流程和预算，不设置每系统 issue 配额。
4. **accepted 定义：** 每 shard 的 1000 指 1000 个 accepted query pairs；资源上限触发后允许且必须显式 `INCOMPLETE`。
5. **runtime 门槛：** discovery 使用 5 个随机交错 pairs；SubmitReady 使用两个独立 session、median ratio ≥3、95% CI 下界 ≥2、absolute delta > A/A noise p99。
6. **语义门禁：** 排除非确定性、无稳定顺序的 `LIMIT/SKIP`、写查询和 A/A 结果不稳定查询；完整 typed result equivalence 不可绕过。
7. **故障边界：** crash/hang 归档为 reliability findings，不计入核心 runtime reports。
8. **issue corpus：** 现有 11 个和未来 issue 均可独立说明；不冻结整个评价实验，但必须记录发现时的方法和系统版本。
9. **规则调度：** 每条 CORE 规则先获得最低探索配额，剩余预算按 validated runtime yield 分配；plan/PROFILE/PMC 不参与权重。
10. **reducer 门禁：** 只有 `REDUCED_VALID` 自动进入 SubmitReady；`REDUCTION_PARTIAL` 必须人工审核。
11. **方言原则：** 只做可证明语义保持的 AST 改写；需删除标签、子句或改变 DISTINCT/ORDER BY 才能运行的查询直接 REJECT 并 top-up。
12. **论文第一贡献：** safety-point 驱动的 BOOL/SYM 语义保持变换与 runtime-first oracle；validation、reducer、方言和稳定运行是支撑贡献。
13. **规则安全层级：** CORE 规则必须产生完整 `SafetyCertificate`；EXPERIMENTAL 规则不能自动生成 SubmitReady issue。
14. **入口与配置：** 只保留一个正式 `config/experiment.yaml`；一次 `graphsymperf experiment run` 默认完成从 generation 到 issue materialization 的全流程。
15. **清理边界：** 保留 11 个 issue 的最小复现证据包，删除除此之外的全部历史实验产物。

| 决策 | 主要落地任务 | 验收证据 |
|---|---|---|
| 1–2 成功与计数口径 | Task 4、10、11 | issue registry、evidence package、paper evaluation 表 |
| 3–4 全系统统一预算与 accepted pairs | Task 2、6、7、13 | 112 tasks、112,000 pair budget、INCOMPLETE 状态 |
| 5–6 runtime 与语义门禁 | Task 3、4 | A/A p99、双 session、typed full-result tests |
| 7 crash/hang 边界 | Task 6 | ReliabilityFinding 与故障注入测试 |
| 8 不冻结 campaign、版本归因 | Task 2、4、10 | immutable run manifest + versioned issue record |
| 9 yield 调度 | Task 8、9 | exploration floor、yield-only sampler contract |
| 10 reducer 门禁 | Task 5 | REDUCED_VALID/PARTIAL/INVALIDATED tests |
| 11 方言无损原则 | Task 7 | CompatibilityDecision 与 no-lossy-rewrite contract |
| 12–13 论文贡献与规则安全 | Task 8、11 | SafetyCertificate、CORE/EXPERIMENTAL、RQ 文档 |
| 14 单入口单配置 | Task 2、11、13 | `config/experiment.yaml` 与唯一 run command |
| 15 证据保留与清理 | Task 10、12 | 11 个 evidence packages + approved deletion manifest |

---

## 0. 已核查基线与重构约束

### 0.1 当前工作树保护

当前工作树包含用户未提交的删除、修改和新增文件。实施时第一条规则是：不得 reset、checkout 或覆盖这些改动。每一阶段开始前执行：

```bash
git status --short
git diff --check
.venv/bin/python -m pytest tests/unit -q
```

本次审计基线为 204 个单元测试通过；它只证明当前工作树的既有测试通过，不证明完整实验可信。

### 0.2 已确认的阻塞问题

1. `experiment/runner.py` 同时承担配置展开、GDsmith 调用、图加载、case 执行、聚合与产物写入，属于浅接口包裹复杂实现。
2. `runner.py` 跨模块导入 `app/mining.py` 的多个私有函数；`app/gdsmith_mining.py` 与 runner 还存在职责回流。
3. 实验 manifest 与基础 `RunConfig` 双重叠加，`per_case_timeout_seconds`、`isolate_system_per_worker` 等字段名义存在但未形成实际运行保证。
4. 当前查询预算按 target 分摊，不等价于“每个 GDBMS、每个 shard 恰好 1000 条”；系统种子未包含 shard，已观察到不同 shard 使用相同 GDsmith seed。
5. 真实 Neo4j 运行在 shard 1 因外部 GDsmith 修改数据库后复用旧连接，`SHOW INDEXES` 触发 defunct connection，单 shard 异常终止了整轮脚本。
6. FalkorDB/LadybugDB 的线程超时不是硬超时；线程已经进入阻塞调用后，线程池退出仍可能等待它结束。
7. 当前 999 个 case 的 shard 约产生 19 GB 产物；按 112 个 shard 线性外推约为 2 TB，必须先重构产物模型才能跑正式矩阵。
8. 主挖掘流程不执行真正的 runtime confirmation；近期修复后 PROFILE 不再伪装成 runtime，因此现有主流程自然会出现 `runtime_confirmed=0`。
9. GDsmith 本地源码有 AgensGraph provider，但没有 Apache AGE provider。AGE 的调用协议不是直接 Cypher socket，而是 PostgreSQL 中的 `ag_catalog.cypher(...)` 表函数。
10. `experiment/triage.py` 当前仍通过 `adapter.profile(...)` 采集 validation 时间，不能充当独立 runtime 复核。
11. `issues/tracker.py::_is_reportable` 当前允许 profile-supported 结果进入 reportable，绕过真正 runtime validation。
12. `reducer/candidate.py` 当前把 runtime ratio 与 profile metric ratio 取最大值作为保留症状，可能保住“计划指标差异”却丢失 runtime 异常。
13. 当前 reducer 使用正则切 clause/WHERE，不做变量依赖分析；graph reducer 捕获异常后未推进 index，存在重复尝试同一语句的卡死风险。
14. 现有 BOOL 规则中多组只是同一恒真/恒假模板的左右操作数变体；SYM 的 exploratory OPTIONAL/投影移动缺乏静态证明，不应进入自动 issue 链。

### 0.3 不可破坏的研究约束

- 语义等价性失败的 query pair 永不进入性能候选。
- 普通执行 runtime 与 PROFILE 内的时间严格分离，不能互换命名或混合统计。
- 冷缓存与热缓存实验分开，不能合并成一个分布。
- timeout、DBMS crash、连接失败和等价性失败使用不同状态，不得都记为“性能异常”。
- 严格规则与探索规则分池；正式 issue 只能来自严格池，除非人工补充证明。
- Apache AGE/AgensGraph 的兼容过滤率、跳过率和失败原因必须成为一等实验指标。
- discovery 的最大 ratio 永不直接写入开发者 issue；issue 只报告独立 validation 的保守估计。
- result 被截断、查询含 volatile function、graph fingerprint 不一致或负对照噪声超标时，validation 必须返回 `inconclusive` 或 `rejected`。
- reducer 输出只有在 reduced pair 重新通过独立 validation 后才可替换原始 query。

---

## 1. 目标架构

### 1.1 分层与目录

```text
src/graph_sym_perf/
├── cli/                         # Interface：唯一命令入口与展示
├── experiment/
│   ├── spec.py                  # ExperimentSpec schema
│   ├── planner.py               # spec -> immutable RunPlan
│   ├── state.py                 # shard 状态机与事件
│   ├── supervisor.py            # 多 DBMS/多 shard 编排、恢复、重试
│   └── worker.py                # 单 DBMS + 单 shard 的进程内生命周期
├── generation/
│   ├── base.py                  # GenerationBackend 接口
│   ├── gdsmith.py               # GDsmith 子进程、固定 commit 与 seed 派生
│   └── corpus.py                # 中立 QueryCorpus/GraphCorpus
├── workload/
│   ├── models.py                # PortableSeed/EquivalentQueryPair/AcceptedPairCorpus
│   └── acceptance.py            # 方言、规则、完整等价性与 accepted-count top-up
├── equivalence/
│   ├── purity.py                # read-only、volatile、顺序稳定性门禁
│   ├── canonical.py             # typed value/row/entity 规范化
│   └── checker.py               # sequence/multiset 完整结果比较
├── runtime/
│   ├── models.py                # paired samples 与证据等级
│   ├── measurement.py           # AB/BA 交错、warmup、timeout
│   ├── oracle.py                # screen/confirm 判定
│   ├── noise.py                 # A/A noise p99 calibration
│   └── statistics.py            # bootstrap CI、方向一致性、噪声门
├── validation/
│   ├── models.py                # ValidationBundle/Report/SubmitReadyIssue
│   ├── graph_snapshot.py        # graph fingerprint 与重载验证
│   ├── validator.py             # 两个独立 session 的 issue 复核
│   └── promotion.py             # 唯一 submit-ready 状态转换
├── reducer/
│   ├── models.py                # ReductionCandidate/Report
│   ├── dependencies.py          # clause/variable/alias/return dependency
│   ├── proposals.py             # AST 层级缩减候选
│   ├── ddmin.py                 # hierarchical delta debugging
│   └── reducer.py               # runtime-preserving reduction loop
├── transforms/
│   ├── registry.py              # RuleSpec 注册表
│   ├── safety_points.py         # 合法注入点与 SafetyCertificate
│   ├── boolean_equivalence/     # BOOL 等价规则与三值逻辑证明
│   └── symmetry/                # 对称性规则
├── sampling/
│   ├── yield_store.py            # 仅保存 accepted/validation-passed 计数
│   └── adaptive.py               # 探索下限 + validated runtime yield
├── explanation/
│   ├── collector.py             # validation 后按需采集
│   └── models.py                # plan/PROFILE/PMC 解释证据
├── dialects/
│   ├── base.py                  # DialectAdapter/Capabilities
│   ├── native.py                # Neo4j/Memgraph/FalkorDB/...
│   ├── agensgraph.py
│   └── apache_age.py
├── artifacts/
│   ├── policy.py                # minimal/candidate/issue 三级保留
│   ├── store.py                 # 内容寻址、压缩、引用
│   └── schema.py                # 可版本化产物 schema
├── issues/
│   ├── registry.py              # pipeline stage、developer status 与版本归因
│   ├── evidence.py              # 公开 issue 的最小证据包 schema
│   └── exporter.py              # 只消费 SubmitReadyIssue
├── reliability/
│   └── findings.py              # crash/hang 独立归档，不进入 runtime 指标
└── adapters/                    # 现有驱动实现，逐步收敛为执行适配器
```

### 1.2 稳定接口

```python
class ExperimentPlanner(Protocol):
    def plan(self, spec: ExperimentSpec) -> RunPlan: ...

class ExperimentSupervisor(Protocol):
    def run(self, plan: RunPlan, *, resume: bool) -> RunSummary: ...

class GdsmithGenerator(Protocol):
    def generate(self, request: GenerationRequest) -> QueryCorpus: ...

class RulePairProvider(Protocol):
    def transform(self, seed: PortableSeed, target: TargetSystem) -> tuple[CertifiedQueryPair, ...]: ...

class AcceptedPairBuilder(Protocol):
    def build(self, request: AcceptedPairRequest, rules: RulePairProvider) -> AcceptedPairCorpus: ...

class DialectAdapter(Protocol):
    @property
    def capabilities(self) -> DialectCapabilities: ...
    def check(self, query: PortableQuery) -> CompatibilityDecision: ...
    def compile(self, query: PortableQuery) -> CompiledQuery: ...
    def load_graph(self, graph: GraphCorpus) -> LoadReport: ...

class RuntimeHarness(Protocol):
    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence: ...

class IssueValidator(Protocol):
    def validate(self, bundle: ValidationBundle) -> IssueValidationReport: ...

class CandidateReducer(Protocol):
    def reduce(self, bundle: ReductionBundle) -> ReductionReport: ...

class IssueExporter(Protocol):
    def export(self, issue: SubmitReadyIssue) -> IssueArtifact: ...

class IssueRegistry(Protocol):
    def record(self, issue: VersionedIssueRecord) -> None: ...
    def update_developer_status(self, issue_id: str, status: DeveloperStatus, evidence_url: str) -> None: ...
```

这些接口必须比实现简单：调用者不需要知道 GDsmith 临时目录、AGE record definition、驱动重连、计时顺序、bootstrap 或产物压缩细节。

### 1.3 运行时优先数据流

```text
GDsmith 生成 PortableSeed
  -> 目标 DBMS DialectCapabilities 检查
  -> 枚举 CORE SafetyPoint 并签发 SafetyCertificate
  -> 生成 base/transformed query pair
  -> 方言编译；不安全改写直接 REJECT 并 top-up
  -> 双方可执行 + 完整 typed result equivalence
  -> accepted_pair_count + 1，直到 1000 或资源上限
  -> 普通 execute 快速筛选
  -> 独立 validation 原始 pair（fresh process/connection/graph reload）
  -> dependency-aware paired AST reducer
  -> 独立 validation reduced pair
  -> REDUCED_VALID -> SubmitReadyIssue
  -> 按需执行 EXPLAIN/PROFILE/PMC
  -> 解释、去重、版本化 registry 与开发者 issue export
```

plan、PROFILE、PMC 不再是 runtime candidate、validation 或 reducer 的判定条件；它们只属于通过 reduced-pair validation 后的解释证据。

Pipeline stage 与开发者状态必须分成两个正交枚举，禁止继续用一个 `support_level` 混合：

```text
PipelineStage:
  DETECTED -> SCREENED -> VALIDATION_PASSED -> REDUCED_VALID ->
  SUBMIT_READY -> SUBMITTED

DeveloperStatus:
  WAITING_REPLY | ACKNOWLEDGED | CONFIRMED | FIXED |
  DUPLICATE | INTENDED_BEHAVIOR | UNINTERESTED | NEEDS_INFORMATION
```

论文中的 `Reported Issue Reports` 统计 `SUBMITTED`；`Developer-Validated Bugs` 只统计 `CONFIRMED` 与 `FIXED`。`WAITING_REPLY` 作为右删失状态单列，不能被当作负例。

---

## 2. 统一实验入口与配置

### 2.1 唯一入口

正式入口统一为：

```bash
graphsymperf experiment run --config config/experiment.yaml
graphsymperf experiment resume --run-id <run-id>
```

`experiment run` 是普通用户唯一的正式入口，默认依次运行 doctor、plan、generation、accepted-pair top-up、runtime screen、validation、reducer、reduced validation、issue materialization 和 summary。`resume` 从原子 checkpoint 恢复同一 immutable run manifest，不能重新解释配置默认值。

以下命令只作为高级诊断接口保留，不出现在 README 主流程：

```bash
graphsymperf experiment doctor --config config/experiment.yaml
graphsymperf experiment plan --config config/experiment.yaml
graphsymperf experiment status --run-id <run-id>
graphsymperf issue validate --candidate <candidate-id>
graphsymperf issue reduce --validation <validation-id>
graphsymperf issue export --issue <submit-ready-id>
graphsymperf workspace audit-clean --dry-run
```

旧 `run-ldbc-mining`、`scripts/run_ldbc_mining.py`、`datasets/ldbc.py`、`querygen/ldbc.py` 和 `exprgen/ldbc.py` 在 neutral graph fixture 与 GDsmith smoke 测试就位后直接删除，不保留 runtime fallback。旧 `run-gdsmith-mining` 与一次性 shell/Python orchestration scripts 在 CLI parity 通过后删除。

### 2.2 配置目录收敛

目标目录：

```text
config/
├── experiment.yaml       # 唯一正式实验配置
├── systems.yaml          # 七个 GDBMS 的非敏感设置
├── rules.yaml            # CORE/EXPERIMENTAL/CONTROL 与调度参数
└── env.example           # 只列环境变量名，不含密钥

tests/fixtures/config/
├── smoke.yaml
├── replay.yaml
├── invalid_unknown_key.yaml
└── failure_injection.yaml
```

- `systems.yaml`：连接环境变量名、方言、能力、默认 timeout、图隔离策略。
- `rules.yaml`：规则安全层级、探索下限、允许的 safety point 类别，不保存连接信息。
- `experiment.yaml`：正式 7×16×1000 accepted-pair 矩阵和全流水线策略。
- smoke、replay、invalid 和 fault-injection 配置是测试夹具，不属于正式配置目录。
- 现有 scale、日期、DBMS 名称拼接出的完整复制配置全部迁移后删除。
- 配置 schema 不再接受 `ldbc`、`ldbc_templates`、`cases_jsonl` 等生成 source；replay 是 issue 子命令的输入，不是生成器。
- 删除 `coverage`、`feedback`、`oracle.enable_structural_diff`、`include_plan_only`、`pmc_new_units` 等查询计划引导字段。
- schema 使用 `extra=forbid` 等价策略拒绝未知字段；配置错误必须在连接 DBMS 前失败。

### 2.3 正式配置语义

```yaml
schema_version: 2
experiment:
  name: main_runtime_first
  seed: 20260710
  feedback_observation_days: 90

targets:
  - neo4j
  - memgraph
  - falkordb
  - agensgraph
  - arcadedb
  - apache_age
  - ladybugdb

matrix:
  shards_per_system: 16

workload:
  accepted_query_pairs_per_shard: 1000
  max_generated_seeds_per_shard: 10000
  max_shard_runtime_hours: 6
  deduplicate_by_ast_hash: true
  max_variants_per_seed: 2

generation:
  backend: gdsmith
  commit_source: vendored_checkout

scheduling:
  max_parallel_generation_systems: 3
  max_parallel_measurement_systems: 1
  max_parallel_shards_per_system: 1
  shard_retry_limit: 3
  heartbeat_seconds: 10
  stale_after_seconds: 90

runtime:
  cache_mode: warm
  screen:
    warmups: 3
    pairs: 5
    ratio: 2.0
    schedule: randomized_ab_ba
  noise:
    probe_queries: 20
    aa_pairs_per_query: 10
    absolute_delta_quantile: 0.99

validation:
  sessions: 2
  warmups_per_session: 5
  pairs_per_session: 20
  schedule: balanced_random
  reconnect_between_sessions: true
  reload_graph_between_sessions: true
  require_complete_equivalence: true
  min_ratio: 3.0
  min_ci_lower_ratio: 2.0
  min_absolute_delta: aa_noise_p99
  bootstrap_resamples: 10000
  require_same_direction_in_both_sessions: true
  max_result_rows: 100000

reducer:
  enabled: true
  max_attempts: 500
  max_seconds: 1800
  require_reduced_validation: true
  auto_submit_status: reduced_valid
  partial_requires_manual_review: true

rules:
  safety_tier: core
  scheduling: exploration_floor_then_validated_yield
  minimum_accepted_pairs_per_core_rule: 20
  yield_update_epoch_accepted_pairs: 100
  forbidden_feedback:
    - plan
    - profile
    - pmc
    - developer_status

artifacts:
  default_level: minimal
  promote_candidate_level: candidate
  promote_issue_level: issue
  shard_budget_mb: 500
```

`generation.commit_source` 只声明解析来源。planner 必须从当前 vendored GDsmith checkout 读取真实 commit 并写入 resolved manifest；GDsmith 工作树 dirty 时拒绝正式 run，smoke fixture 可显式允许。

runtime 阈值是已批准的 issue-grade 规则，不宣称为所有系统的统计定律。每个 `(DBMS, shard)` 先构建 20 个 accepted pairs，将其中 20 条 base query 各执行 10 个 A/A pair，得到至少 200 个绝对差值并取 p99 作为当前 shard 的噪声门；这 20 个 accepted pairs 在 noise preflight 通过后再进入正常 screen。不足 200 个有效样本、A/A 结果不稳定或执行顺序明显偏置时，该 shard preflight 失败。

规则权重只在每 100 个 accepted pairs 的 epoch 边界更新，使用截至该边界已经完成的 `validation_passed / accepted`，并用 Laplace smoothing `(passed + 1) / (accepted + 2)` 避免冷启动为零。未完成 validation 的候选不提前计入收益；confirmed/fixed、plan、PROFILE 和 PMC 永不参与该分数。

### 2.4 预算和 seed 的硬验收

`experiment plan` 必须在不连接 DBMS 的情况下输出并验证：

- 7 个 target。
- 每 target 16 个 shard，共 112 个 shard task。
- 每个 completed shard 恰好 1000 个 accepted query pairs。
- 全部完成时总计 112,000 个 accepted query pairs。
- `seed = H(experiment_seed, dbms_id, shard_id)`，112 个 shard seed 无重复。
- `accepted_pair_count` 只有在 base/transformed 均通过方言编译、执行成功、完整结果等价且没有 health event 时增加。
- 生成器不足时继续 top-up；达到 10,000 seeds 或 6 小时仍不足 1000 时标记 `INCOMPLETE`，保留精确 reject reason 分布。
- generated seeds、dialect accepted、rule applicable、equivalence passed 和 accepted pairs 分列统计，不能混称。
- `INCOMPLETE` shard 不阻止其他 shard 继续，但不能进入 completed 分母，也不能被自动重跑到无限次。

---

## 3. 分阶段实施任务

### Task 1：保护基线并建立契约测试

**Files**

- Create: `tests/contracts/test_current_experiment_contract.py`
- Create: `tests/contracts/test_artifact_schema_contract.py`
- Create: `tests/contracts/test_issue_status_contract.py`
- Create: `tests/contracts/test_layer_boundaries.py`
- Create: `docs/architecture-baseline.md`
- Create: `docs/issue-corpus-baseline.md`
- Modify: `pyproject.toml`

**Steps**

- [ ] **Step 1:** 记录当前 `git status --short`、单元测试结果、配置清单、入口清单和目录体积；不复制 19 GB 原始产物。
- [ ] **Step 2:** 为现有 manifest 解析、target 列表、case 状态和 artifact 字段建立 characterization tests。
- [ ] **Step 3:** 把现有 11 个公开 issue 按用户提供的状态写入只读 baseline 表：4 fixed、2 confirmed、2 uninterested、3 waiting reply；保存 URL、DBMS、当前本地证据路径和缺失证据，不伪造维护者结论。
- [ ] **Step 4:** 将当前未提交文件路径（包括用户已删除的 `BUG list/*`）写入本地实施日志；后续不得 checkout、reset 或自动恢复这些路径。
- [ ] **Step 5:** 写状态契约测试，断言 `ReportedIssueReport`、`DeveloperValidatedBug`、`PipelineStage`、`DeveloperStatus` 是不同统计对象。
- [ ] **Step 6:** 写分层契约测试：`runtime/equivalence/transforms` 不得导入 CLI、app、experiment 或具体 adapter；CLI 只能依赖 experiment/issues 公共接口；experiment 不得导入 app 私有函数。
- [ ] **Step 7:** 测试红灯时只修正测试对现状的误读，不在本任务修改生产行为。
- [ ] **Step 8:** 运行全部 unit 与 contract tests。

**Gate**

```bash
.venv/bin/python -m pytest tests/unit tests/contracts -q
git diff --check
```

### Task 2：建立单一 ExperimentSpec 和确定性 RunPlan

**Files**

- Create: `src/graph_sym_perf/experiment/spec.py`
- Create: `src/graph_sym_perf/experiment/planner.py`
- Create: `src/graph_sym_perf/experiment/plan_models.py`
- Create: `tests/unit/test_experiment_spec.py`
- Create: `tests/unit/test_experiment_planner.py`
- Create: `config/experiment.yaml`
- Create: `config/systems.yaml`
- Create: `config/rules.yaml`
- Create: `config/env.example`
- Modify: `src/graph_sym_perf/experiment/manifest.py`
- Modify: `src/graph_sym_perf/config/loader.py`

**Steps**

- [ ] **Step 1:** 先写失败测试：唯一正式 `config/experiment.yaml` 展开为 112 个 task，总 accepted-pair budget 为 112,000。
- [ ] **Step 2:** 先写失败测试：每个 `(dbms, shard)` seed 唯一且重复 plan 得到完全相同结果。
- [ ] **Step 3:** 先写失败测试：0 shard、0 pair、重复 target、未知字段、非 GDsmith generator、LDBC source 和预算溢出都在 plan 阶段失败。
- [ ] **Step 4:** 实现不可变 `ExperimentSpec`、`ShardTask`、`RunPlan`；所有默认值在 schema 中只有一个来源。
- [ ] **Step 5:** 将 `RunConfig` 与 manifest 的叠加逻辑改为显式迁移函数；禁止运行时静默 overlay。
- [ ] **Step 6:** planner 读取当前 Git commit、GDsmith commit、规则 schema hash、validator/reducer protocol version 和七个 DBMS 目标；生成 `resolved_manifest.json`，敏感环境变量只记录 present/missing。
- [ ] **Step 7:** 旧配置只通过离线 migration command 转换；runtime 主入口只接受 schema v2。smoke/replay/fault-injection 移入 `tests/fixtures/config/`。
- [ ] **Step 8:** 删除死字段；`per_case_timeout_seconds` 和 `isolate_system_per_worker` 在 worker 中成为真实行为，`write_issue_drafts` 被 typed exporter 取代。

**Gate**

```bash
.venv/bin/python -m pytest tests/unit/test_experiment_spec.py tests/unit/test_experiment_planner.py -q
graphsymperf experiment plan --config config/experiment.yaml
```

### Task 3：实现只测普通执行的 RuntimeHarness

**Files**

- Create: `src/graph_sym_perf/runtime/__init__.py`
- Create: `src/graph_sym_perf/runtime/models.py`
- Create: `src/graph_sym_perf/runtime/measurement.py`
- Create: `src/graph_sym_perf/runtime/statistics.py`
- Create: `src/graph_sym_perf/runtime/noise.py`
- Create: `src/graph_sym_perf/runtime/oracle.py`
- Test: `tests/unit/runtime/test_measurement.py`
- Test: `tests/unit/runtime/test_statistics.py`
- Test: `tests/unit/runtime/test_oracle.py`
- Modify: `src/graph_sym_perf/confirm/issue_runtime_check.py`
- Replace after parity: `src/graph_sym_perf/confirm/runtime.py`

**Interfaces**

- Consumes: `EquivalentQueryPair`、`RuntimePolicy` 和只暴露 `execute(...)` 的 executor adapter。
- Produces: `RuntimeEvidence`，包含 paired samples、median ratio、absolute delta、relative MAD、方向一致性、bootstrap CI、timeout lower bound 和明确 verdict。

```python
class RuntimeVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"

@dataclass(frozen=True)
class RuntimeEvidence:
    verdict: RuntimeVerdict
    query_a_hash: str
    query_b_hash: str
    paired_samples: tuple[PairedSample, ...]
    median_ratio: float | None
    median_delta_ms: float | None
    ci_ratio_low: float | None
    ci_ratio_high: float | None
    direction_consistency: float
    relative_mad: float | None
    reason_codes: tuple[str, ...]
```

- [ ] **Step 1:** 写失败测试：discovery 的 5 pairs 使用随机但近似平衡的 3/2 AB/BA；validation 的 20 pairs 恰好 10 AB、10 BA；固定 seed 重放顺序一致。
- [ ] **Step 2:** 写失败测试，断言 warmup 不进入样本，且 fake executor 的 `profile` 方法被调用时立即失败。
- [ ] **Step 3:** 写失败测试，断言 one-sided repeated timeout 形成 lower-bound evidence，双方 timeout 返回 `inconclusive`。
- [ ] **Step 4:** 写失败测试：20 条 A/A probe query × 每条 10 pairs 形成至少 200 个有效 delta；绝对差 p99 成为当前 DBMS 的 noise floor，样本不足或顺序偏置时 preflight 失败。
- [ ] **Step 5:** 实现 paired schedule、`median(t_slow/t_fast)`、`median(t_slow-t_fast)`、方向一致性和 10,000 次 bootstrap CI；slow/fast 方向必须从 discovery 固定并在两个 validation session 中保持。
- [ ] **Step 6:** 实现严格 verdict：合并 median ratio ≥3.0、95% CI lower ≥2.0、median absolute delta > `max(aa_noise_p99, 10 × timer_resolution)`；任一条件不足返回 FAIL/INCONCLUSIVE，不能由 profile 覆盖。
- [ ] **Step 7:** 把现有 `issue_runtime_check` 的统计实现收敛到 `RuntimeHarness`，删除 mean ratio、单次 maximum 和固定毫秒阈值作为 promotion 判据。
- [ ] **Step 8:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/runtime -q
```

预期：全部通过，fake executor 记录中只有 `execute`。

### Task 4：建立独立 IssueValidator 与不可绕过的发布状态机

**Files**

- Create: `src/graph_sym_perf/equivalence/purity.py`
- Create: `src/graph_sym_perf/equivalence/canonical.py`
- Create: `src/graph_sym_perf/equivalence/checker.py`
- Create: `src/graph_sym_perf/validation/models.py`
- Create: `src/graph_sym_perf/validation/graph_snapshot.py`
- Create: `src/graph_sym_perf/validation/validator.py`
- Create: `src/graph_sym_perf/validation/promotion.py`
- Create: `src/graph_sym_perf/issues/registry.py`
- Create: `src/graph_sym_perf/issues/exporter.py`
- Test: `tests/unit/equivalence/test_purity.py`
- Test: `tests/unit/equivalence/test_canonical.py`
- Test: `tests/unit/equivalence/test_checker.py`
- Test: `tests/unit/validation/test_promotion.py`
- Test: `tests/unit/issues/test_registry.py`
- Test: `tests/unit/issues/test_dedup.py`
- Test: `tests/integration/test_issue_validation.py`
- Modify: `src/graph_sym_perf/issues/tracker.py`
- Modify: `src/graph_sym_perf/experiment/triage.py`

**Interfaces**

- Consumes: discovery `CandidateBundle`，其中 query、parameters、graph snapshot、DBMS version 和配置均有 content hash。
- Produces: `IssueValidationReport`；只有 `promote(report, reduction_report)` 能构造 `SubmitReadyIssue`。

```python
class PipelineStage(str, Enum):
    DETECTED = "detected"
    SCREENED = "screened"
    VALIDATION_PASSED = "validation_passed"
    REDUCED_VALID = "reduced_valid"
    SUBMIT_READY = "submit_ready"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"

class DeveloperStatus(str, Enum):
    WAITING_REPLY = "waiting_reply"
    ACKNOWLEDGED = "acknowledged"
    CONFIRMED = "confirmed"
    FIXED = "fixed"
    DUPLICATE = "duplicate"
    INTENDED_BEHAVIOR = "intended_behavior"
    UNINTERESTED = "uninterested"
    NEEDS_INFORMATION = "needs_information"

@dataclass(frozen=True)
class ValidationSession:
    session_id: str
    process_id: int
    graph_fingerprint: str
    dbms_fingerprint: str
    evidence: RuntimeEvidence
    negative_controls: tuple[RuntimeEvidence, ...]

@dataclass(frozen=True)
class IssueValidationReport:
    stage: PipelineStage
    equivalence: EquivalenceEvidence
    sessions: tuple[ValidationSession, ...]
    discovery_ratio: float | None
    validated_ratio: float | None
    magnitude_stability: str
    reason_codes: tuple[str, ...]
```

- [ ] **Step 1:** 写失败测试：`profile_supported`、plan difference 或 PMC novelty 无论多强，都不能构造 `SubmitReadyIssue`。
- [ ] **Step 2:** 写 purity 失败测试：拒绝 write clauses、`rand/random`、当前时间、无确定性 `ORDER BY` 的 `LIMIT/SKIP`、无稳定输入顺序的 `collect()`、内部 ID 函数、NaN/无穷参与的浮点变换和 adapter 声明的 volatile function。
- [ ] **Step 3:** 写 typed equivalence 失败测试：无 `ORDER BY` 按完整 row multiset 比较并保留重复数；有 `ORDER BY` 按完整 sequence 比较；NULL、missing、空列表、空字符串、整数/浮点、list/map、vertex/edge 不得字符串化混同。
- [ ] **Step 4:** 写失败测试：结果超过 100,000 rows、adapter 表示截断、任一侧 error/timeout、A/A 结果不稳定或 typed result 不等价时，validation 不得 pass。hash/sample 只作快速拒绝，不能替代最终完整比较。
- [ ] **Step 5:** 写失败测试：每个 session 前必须 reset/load 同一 graph snapshot、验证 fingerprint、关闭并重建 adapter；缺任一步返回 `invalid`。
- [ ] **Step 6:** 写失败测试：两个 session 必须同方向；合并 median ratio <3、CI lower <2、absolute delta ≤ A/A p99 或任一 session 出现 health event 时不得 promotion。
- [ ] **Step 7:** 实现两个独立 measurement session。这里的“独立”定义为新 OS 进程、新 driver 连接、重新加载相同 graph snapshot；不夸大为独立机器复现。每 session 5 次 warmup + 20 个 balanced pairs。
- [ ] **Step 8:** discovery ratio 只用于计算 `magnitude_stability`。开发者 issue 的 ratio 使用两个 validation session 的合并 paired median 与 CI，绝不使用 discovery maximum。
- [ ] **Step 9:** 实现 `IssueRegistry`：pipeline stage 与 developer status 正交；记录 `detector_version`、`rule_version`、`validator_version`、`reducer_version`、DBMS version、发现日期和 status evidence URL。
- [ ] **Step 10:** 实现两级去重：先按 DBMS/version + reduced pair canonical AST hash + direction 精确去重，再按 rule、SafetyCertificate site class、normalized pattern/predicate skeleton 生成 possible-duplicate cluster；possible duplicate 必须人工裁决后才能提交。
- [ ] **Step 11:** 同一 DBMS 中仅常量、变量名、标签名变化的 query pair 只能形成一个 ReportedIssueReport；跨 DBMS 可形成多个 reports，但在论文中另报 bug-family grouping。
- [ ] **Step 12:** 修改 `issues/tracker.py`，`_is_reportable` 只接受 `stage is PipelineStage.SUBMIT_READY`；删除 profile/runtime 混合 predicate。
- [ ] **Step 13:** issue exporter 只接受静态类型 `SubmitReadyIssue`；raw dict、candidate 和 validation report 直接传入必须在类型检查和 runtime check 中失败。
- [ ] **Step 14:** `ReportedIssueReports` 统计 submitted；`DeveloperValidatedBugs` 只统计 confirmed/fixed；waiting reply 在 90 天观察窗内保持右删失，不计为负例。
- [ ] **Step 15:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/equivalence tests/unit/validation tests/unit/issues tests/integration/test_issue_validation.py -q
```

预期：包含 profile-only 不可发布、graph mismatch、截断结果、volatile query、负对照噪声和双 session 的回归测试。

### Task 5：用 dependency-aware AST reducer 替换正则 reducer

**Files**

- Create: `src/graph_sym_perf/reducer/models.py`
- Create: `src/graph_sym_perf/reducer/dependencies.py`
- Create: `src/graph_sym_perf/reducer/proposals.py`
- Create: `src/graph_sym_perf/reducer/ddmin.py`
- Create: `src/graph_sym_perf/reducer/reducer.py`
- Test: `tests/unit/reducer/test_dependencies.py`
- Test: `tests/unit/reducer/test_proposals.py`
- Test: `tests/unit/reducer/test_ddmin.py`
- Test: `tests/integration/test_runtime_preserving_reducer.py`
- Replace: `src/graph_sym_perf/reducer/candidate.py`

**Interfaces**

- Consumes: 已通过原始 pair validation 的 `ReductionBundle` 与 `SymptomPredicate`。
- Produces: `ReductionReport`，其中 original/reduced AST hash、字符数、AST node 数、接受/拒绝步骤、最终 validation report 均可追溯。

```python
class SymptomPredicate(Protocol):
    def evaluate(self, pair: EquivalentQueryPair) -> RuntimeEvidence: ...

class ReductionStatus(str, Enum):
    REDUCED_VALID = "reduced_valid"
    REDUCTION_PARTIAL = "reduction_partial"
    REDUCTION_INVALIDATED = "reduction_invalidated"

@dataclass(frozen=True)
class ReductionReport:
    status: ReductionStatus
    original_pair: EquivalentQueryPair
    reduced_pair: EquivalentQueryPair
    accepted_steps: tuple[ReductionStep, ...]
    rejected_steps: tuple[ReductionStep, ...]
    final_validation: IssueValidationReport | None
```

- [ ] **Step 1:** 写失败测试，复现当前 graph reducer 在 reload exception 时不推进 index 的问题；新 ddmin 必须有 attempt id、deadline 和单调进度。
- [ ] **Step 2:** 写失败测试：只保留 PROFILE/db_hits 差异但 runtime 消失的 proposal 必须拒绝。
- [ ] **Step 3:** 写失败测试：删除 clause 后产生未绑定变量、删除 RETURN 依赖、改变输出列、破坏 metamorphic rule witness 的 proposal 在执行前被 dependency checker 拒绝。
- [ ] **Step 4:** 建立 Cypher dependency graph：clause 定义/使用变量、WHERE 依赖、WITH/RETURN aliases、ORDER/LIMIT barriers、path/relationship variables。
- [ ] **Step 5:** 按层级生成 proposal：whole clause → pattern atom → projection item → predicate subtree → literal/list item → graph setup chunk；每一步同时维护 pair relation 和 rule witness。
- [ ] **Step 6:** 实现 hierarchical delta debugging。先按 chunk 删除，再逐步细化；cache key 使用 `graph_hash + query_a_hash + query_b_hash + policy_hash`，每次改写同时作用于 query pair 或显式维护 rule witness。
- [ ] **Step 7:** 快速 reducer predicate 依次执行 parse/type/dependency、完整等价性和 5-pair runtime screen；不满足任一门禁立即拒绝该 proposal，避免每步都运行完整 validation。
- [ ] **Step 8:** reducer 维护原子 `best-so-far`、attempt id、deadline 和 monotonic progress；达到 500 attempts 或 30 分钟时返回 `REDUCTION_PARTIAL`，不得假装最小化完成。
- [ ] **Step 9:** 没有任何单个允许的 AST 删除仍能保持症状时，才标记局部 1-minimal；随后调用完整 IssueValidator。通过后为 `REDUCED_VALID`，不等价或 runtime 消失为 `REDUCTION_INVALIDATED`。
- [ ] **Step 10:** promotion 只自动接受 `REDUCED_VALID`；`REDUCTION_PARTIAL` 保存供人工审核，人工批准记录 reviewer、时间与理由后才可提交。
- [ ] **Step 11:** 拒绝把 `RETURN count(*)` 作为无条件缩减，因为它改变 observable result 和执行工作；只有在专门证明原 issue 与 projection 无关的实验模式中才允许，且不进入自动 issue。
- [ ] **Step 12:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/reducer tests/integration/test_runtime_preserving_reducer.py -q
```

验收：至少覆盖 clause、pattern、predicate 三层缩减；`REDUCED_VALID` query 更短、达到局部 1-minimal 并重新通过完整独立 validation；partial 不能自动 export。

### Task 6：用进程隔离的 supervisor/worker 替换巨型 runner

**Files**

- Create: `src/graph_sym_perf/experiment/state.py`
- Create: `src/graph_sym_perf/experiment/supervisor.py`
- Create: `src/graph_sym_perf/experiment/worker.py`
- Create: `src/graph_sym_perf/experiment/process_control.py`
- Create: `src/graph_sym_perf/reliability/findings.py`
- Test: `tests/unit/test_experiment_state.py`
- Test: `tests/integration/test_supervisor_recovery.py`
- Test: `tests/integration/test_worker_hard_timeout.py`
- Modify then delete after parity: `src/graph_sym_perf/experiment/runner.py`

**Interfaces**

- Consumes: immutable `RunPlan`。
- Produces: `RunSummary` 和每 shard 的 append-only event stream；不返回 adapter、GDsmith process 或内部 mutable state。

- [ ] **Step 1:** 定义 shard 状态机：`PLANNED -> GENERATING -> LOADING -> BUILDING_ACCEPTED_PAIRS -> SCREENING -> VALIDATING -> REDUCING -> COMPLETED`；资源上限不足 1000 时为 `INCOMPLETE`，基础设施失败分 `FAILED_RETRYABLE` 与 `FAILED_FINAL`。
- [ ] **Step 2:** 每个 `(DBMS, shard)` 使用独立 OS 进程和进程组，deadline 后杀死进程组。
- [ ] **Step 3:** 每个 shard 写 heartbeat、append-only events 和原子 checkpoint。
- [ ] **Step 4:** GDsmith 完成后关闭旧 adapter；图加载和执行阶段建立新连接并验证 DBMS fingerprint。
- [ ] **Step 5:** adapter 在一个 worker 内复用，但阶段边界支持 reconnect；禁止每 case 新建连接。
- [ ] **Step 6:** 同一 DBMS 默认 `max_parallel_shards=1`，同一主机默认 `max_parallel_measurement_systems=1`；仅 GDsmith generation 可并行 3 个系统。只有独立主机和 graph/database/container slot 才允许提高测量并行度，并必须重新建立 A/A noise floor。
- [ ] **Step 7:** 实现错误枚举：`QUERY_TIMEOUT`、`DRIVER_TIMEOUT`、`WORKER_HUNG`、`WORKER_CRASHED`、`DBMS_UNAVAILABLE`、`DBMS_CRASHED`、`INFRASTRUCTURE_ERROR`。发生 DBMS restart/health event 的在途 runtime 样本全部作废。
- [ ] **Step 8:** worker 或 DBMS 自动恢复最多 3 次；超过次数只终止当前 shard，supervisor 继续其他任务。resume 按 query/pair content hash 去重，保证 exactly-once counting。
- [ ] **Step 9:** 可复现 crash/hang 写入 `ReliabilityFinding`，保存 query、graph、版本与重现次数；禁止进入 runtime candidate、30+ issue 计数或 runtime paper table。
- [ ] **Step 10:** 用永久阻塞 adapter、无法取消的 driver call、GDsmith crash、DBMS restart、磁盘写满和 SIGKILL 做故障注入。
- [ ] **Step 11:** 运行：

```bash
.venv/bin/python -m pytest tests/integration/test_supervisor_recovery.py tests/integration/test_worker_hard_timeout.py -q
```

### Task 7：把 GDsmith 固化为唯一生成器并完成方言 seam

**Files**

- Create: `src/graph_sym_perf/generation/corpus.py`
- Create: `src/graph_sym_perf/generation/gdsmith.py`
- Create: `src/graph_sym_perf/workload/models.py`
- Create: `src/graph_sym_perf/workload/acceptance.py`
- Create: `src/graph_sym_perf/dialects/base.py`
- Create: `src/graph_sym_perf/dialects/native.py`
- Create: `src/graph_sym_perf/dialects/agensgraph.py`
- Create: `src/graph_sym_perf/dialects/apache_age.py`
- Test: `tests/unit/test_gdsmith_generation.py`
- Test: `tests/unit/workload/test_acceptance.py`
- Test: `tests/contracts/test_dialect_capabilities.py`
- Test: `tests/contracts/test_dialect_no_lossy_rewrite.py`
- Modify: `src/graph_sym_perf/gdsmith/compatibility.py`
- Delete: `src/graph_sym_perf/datasets/ldbc.py`
- Delete: `src/graph_sym_perf/querygen/ldbc.py`
- Delete: `src/graph_sym_perf/exprgen/ldbc.py`
- Delete: `src/graph_sym_perf/datasets/__init__.py` if empty after LDBC removal
- Delete: `src/graph_sym_perf/querygen/__init__.py` if empty after LDBC removal
- Delete: `src/graph_sym_perf/exprgen/__init__.py` if empty after LDBC removal
- Delete: `scripts/run_ldbc_mining.py`
- Delete after CLI parity and cleanup approval: `scripts/run_gdsmith_mining.py`
- Delete after CLI parity and cleanup approval: `scripts/run_neo4j_10x1000_reliable.py`
- Delete after CLI parity and cleanup approval: `scripts/cluster_candidates.py`
- Delete after CLI parity and cleanup approval: `scripts/export_review_candidates.py`
- Delete after CLI parity and cleanup approval: `scripts/replay_symmetry_candidate.py`
- Delete after schema-v2 migration: `config/graph_sym_perf.yaml`
- Delete after schema-v2 migration: `config/graph_sym_perf_10000.yaml`
- Delete after schema-v2 migration: `config/graph_sym_perf_2000.yaml`
- Delete after schema-v2 migration: `config/graph_sym_perf_500_longtimeout.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_bool_redundancy.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_bool_static.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_both_feedback.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_both_static.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_symmetry.yaml`
- Delete after fixture/evidence migration: `config/experiments/ablation_symmetry_static.yaml`
- Delete after fixture/evidence migration: `config/experiments/calibration.yaml`
- Delete after fixture/evidence migration: `config/experiments/exploratory_symmetry.yaml`
- Delete after fixture/evidence migration: `config/experiments/falkordb_10k.yaml`
- Delete after fixture/evidence migration: `config/experiments/feedback_guided.yaml`
- Delete after fixture/evidence migration: `config/experiments/feedback_static.yaml`
- Delete after fixture/evidence migration: `config/experiments/local_ladybugdb_smoke.yaml`
- Delete after fixture/evidence migration: `config/experiments/local_multidb_gdsmith_smoke.yaml`
- Delete after fixture/evidence migration: `config/experiments/local_multidb_smoke.yaml`
- Delete after fixture/evidence migration: `config/experiments/local_neo4j_gdsmith_smoke.yaml`
- Delete after fixture/evidence migration: `config/experiments/local_single_dbms_smoke.yaml`
- Delete after fixture/evidence migration: `config/experiments/main_effectiveness.yaml`
- Delete after fixture/evidence migration: `config/experiments/main_effectiveness_v2.yaml`
- Delete after fixture/evidence migration: `config/experiments/main_effectiveness_v2_gap_targets.yaml`
- Delete after fixture/evidence migration: `config/experiments/memgraph_10k.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_10k.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_10k_128_shards10.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_10k_256.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_10k_256_shards10.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_10k_512.yaml`
- Delete after fixture/evidence migration: `config/experiments/neo4j_5k.yaml`
- Delete after fixture/evidence migration: `config/experiments/oracle_reliability.yaml`
- Delete after fixture/evidence migration: `config/experiments/replay_known.yaml`
- Delete after fixture/evidence migration: `config/experiments/replay_known_cases.jsonl`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/apache_age_2428_reproducer_graph.cypher.txt`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/arcadedb_4405_reproducer_graph.cypher.txt`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/falkordb_2081_reproducer_graph.cypher.txt`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/memgraph_4208_reproducer_graph.cypher.txt`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/neo4j_13783_reproducer_graph.cypher.txt`
- Delete after issue evidence migration: `config/experiments/replay_known_setups/neo4j_13814_reproducer_graph.cypher.txt`
- Delete after fixture/evidence migration: `config/experiments/symmetry_effectiveness.yaml`
- Delete after fixture/evidence migration: `config/experiments/triage_top_candidates.yaml`
- Delete after CLI parity: one-off mining/smoke/reliable-run scripts replaced by `graphsymperf experiment run`
- Modify: `src/graph_sym_perf/adapters/base.py`
- Modify: `src/graph_sym_perf/adapters/common_cypher.py`
- Modify: `src/graph_sym_perf/adapters/agensgraph.py`
- Modify: `src/graph_sym_perf/adapters/apache_age.py`
- Modify: `src/graph_sym_perf/adapters/ladybugdb.py`
- Modify: `src/graph_sym_perf/app/__init__.py`
- Modify: `src/graph_sym_perf/app/mining.py`
- Modify: `src/graph_sym_perf/cli/__main__.py`
- Modify: `src/graph_sym_perf/config/loader.py`
- Modify: `src/graph_sym_perf/core/models.py`
- Modify: `src/graph_sym_perf/experiment/manifest.py`
- Modify: `src/graph_sym_perf/experiment/runner.py`
- Modify: `tests/unit/test_agensgraph_adapter.py`
- Modify: `tests/unit/test_bool_redundancy_family.py`
- Modify: `tests/unit/test_transform_registry.py`

**Interfaces**

- `GdsmithGenerator.generate(request) -> QueryCorpus` 是唯一 generation seam；不创建第二个 generator adapter。
- `DialectAdapter.load_graph(GraphCorpus)` 替代 `load_mini_ldbc`，测试使用 neutral graph fixture。
- `DialectAdapter.check(PortableQuery) -> CompatibilityDecision` 返回 `ACCEPT | REWRITE | REJECT` 和稳定 reason code；不能只返回 bool。
- `AcceptedPairBuilder.build(AcceptedPairRequest, RulePairProvider) -> AcceptedPairCorpus` 负责 top-up，只有完成方言编译、双方可执行和完整结果等价的 pair 才计数；本任务用 fake provider 测试，Task 8 接入真实 SafetyCertificate rule provider。

- [ ] **Step 1:** 写 schema 失败测试：`generation.backend` 不是 `gdsmith`、source 出现 `ldbc`/`ldbc_templates` 时立即报配置错误。
- [ ] **Step 2:** 定义 `GraphCorpus`、`PortableQuery`、`QueryCorpus`，使生成数据库与目标执行数据库解耦。
- [ ] **Step 3:** 定义 `CompatibilityDecision(status, reason_code, details, rewritten_ast)` 与 `CompiledQuery(cypher_core, execution_envelope, result_schema)`；所有 reason code 使用枚举并进入 per-shard rejection metrics。
- [ ] **Step 4:** 写无损契约测试：禁止通过删除 label、pattern、WHERE、projection、DISTINCT/ORDER BY 或替换不等价函数把 REJECT 变成 ACCEPT。只有 proof-backed AST rewrite 可返回 REWRITE。
- [ ] **Step 5:** 实现 accepted-count top-up：GDsmith seed → AST hash 去重 → CORE rule + SafetyCertificate → pair compile → 双方 execute → typed equivalence；不足 1000 继续生成，10,000 seeds 或 6 小时后返回 INCOMPLETE。
- [ ] **Step 6:** seed 派生纳入 experiment、DBMS 与 shard；检测跨 shard 重复 seed AST hash 和 accepted pair hash。
- [ ] **Step 7:** 删除 LDBC CLI、dataset、query/expr generator 和 `load_mini_ldbc`；AgensGraph 等 adapter tests 迁移到 neutral `GraphCorpus`。
- [ ] **Step 8:** Apache AGE 使用隔离的 AgensGraph GDsmith generation proxy，但 graph/query 先转 neutral corpus 再编译到 AGE；manifest 记录 `generation_backend=agensgraph_proxy`，不宣称原生 provider 覆盖。
- [ ] **Step 9:** AGE compiler 从 AST projection 生成 `ag_catalog.cypher(...) AS (... agtype)` result schema；`RETURN *` 必须从作用域确定完整列，否则 REJECT。使用动态 PostgreSQL dollar-quote tag，不修改原 Cypher 中的 `$$` 文本。
- [ ] **Step 10:** AgensGraph 使用固定 GDsmith commit、每连接设置 `graph_path`、预建 VLABEL/ELABEL 和 single-label capability profile；graph/query 含多标签时 REJECT，不得像当前 `_primary_node_label()` 一样静默只保留第一个标签。
- [ ] **Step 11:** 参数只通过 typed literal renderer 或目标驱动正式参数协议处理；禁止正则字符串替换造成引号、转义、map/list 或类型漂移。
- [ ] **Step 12:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/test_gdsmith_generation.py tests/unit/workload tests/contracts/test_dialect_capabilities.py tests/contracts/test_dialect_no_lossy_rewrite.py -q
! rg -n "load_mini_ldbc|run-ldbc-mining|run_ldbc_mining|generation:[[:space:]]*ldbc" src scripts config README.md
```

第二条命令预期无输出；负面 schema tests 和论文 related-work 可以保留 LDBC 字面量，但不能保留可调用能力。

### Task 8：重构 BOOL/SYM 规则、SafetyCertificate 与 yield 调度

**Files**

- Create: `src/graph_sym_perf/transforms/rule_models.py`
- Create: `src/graph_sym_perf/transforms/safety_points.py`
- Create: `src/graph_sym_perf/transforms/boolean_equivalence/rules.py`
- Create: `src/graph_sym_perf/transforms/boolean_equivalence/proofs.py`
- Create: `src/graph_sym_perf/transforms/symmetry/rules.py`
- Create: `src/graph_sym_perf/sampling/yield_store.py`
- Create: `src/graph_sym_perf/sampling/adaptive.py`
- Test: `tests/unit/transforms/test_rule_registry.py`
- Test: `tests/unit/transforms/test_safety_points.py`
- Test: `tests/property/test_boolean_rule_soundness.py`
- Test: `tests/property/test_symmetry_rule_soundness.py`
- Test: `tests/unit/test_adaptive_sampler.py`
- Replace: `src/graph_sym_perf/transforms/bool_redundancy/family.py`
- Split: `src/graph_sym_perf/transforms/symmetry/family.py`

**Interfaces**

- `RuleRegistry.rules_for(dbms, tier) -> tuple[RuleSpec, ...]`。
- `enumerate_safety_points(ast, rule, capabilities) -> tuple[SafetyCertificate, ...]`。
- `AdaptiveRuleSampler.select(dbms, shard, case_index, yield_snapshot) -> RuleSelection`；`yield_snapshot` 只包含 accepted 与 validation-passed 计数，不包含 plan/PROFILE/PMC/developer status。

- [ ] **Step 1:** 把 `RuleSpec` 的 safety tier、proof theorem、purity requirements、dialect capabilities、site classes、submit eligibility 和 control/stress 角色变成必填字段；tier 限定为 CORE、EXPERIMENTAL、CONTROL、REMOVED。
- [ ] **Step 2:** 定义 `SafetyCertificate(rule_id, ast_path, variables_in_scope, expression_type, nullability, clause_context, preconditions, proof_id, capability_hash)`；CORE rule 没有 certificate 时不得生成 query pair。
- [ ] **Step 3:** 枚举所有合法 MATCH graph-pattern component、mandatory MATCH clause、WHERE boolean subtree 和 bound variable，删除“第一个 MATCH/第一个变量”的隐式选择。
- [ ] **Step 4:** CORE rule 任一动态等价 mismatch 时，把 `(DBMS, rule, rule_version)` 标为 quarantined 并保存 counterexample；本 shard 后续不再执行该版本。
- [ ] **Step 5:** 实现探索下限：每条启用 CORE rule 在每 shard 尽力获得至少 20 个 accepted pairs；不适用次数单列，不能用强行改写凑配额。
- [ ] **Step 6:** planner 在每个 DBMS capability profile 上验证 `applicable_core_rule_count × 20 <= 1000`；若不成立则配置无效，不能在运行中静默压缩探索下限。
- [ ] **Step 7:** 实现 epoch 调度：探索下限满足后，每 100 accepted pairs 用 `(validation_passed + 1)/(accepted + 2)` 更新剩余预算权重。任何 plan/PROFILE/PMC/developer-status 字段进入 sampler 输入时测试失败。
- [ ] **Step 8:** EXPERIMENTAL 候选可以保存和人工研究，但 promotion 拒绝没有 CORE SafetyCertificate 的自动 SubmitReady；CONTROL 永不 submit。
- [ ] **Step 9:** 实现本计划“第 6 节”的 BOOL/SYM 保留、合并、隔离和新增清单。
- [ ] **Step 10:** 每条 CORE rule 做三值逻辑 truth-table、AST/RA proof、property-based graph/query 测试和每 DBMS dialect contract。
- [ ] **Step 11:** pattern decomposition/fusion 只有在能证明 relationship uniqueness scope 不变时才可升级 CORE；默认 EXPERIMENTAL，不依赖单一图上的动态等价侥幸放行。
- [ ] **Step 12:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/transforms tests/property tests/unit/test_adaptive_sampler.py -q
```

### Task 9：删除查询计划引导，只保留被动 ExplanationEvidence

**Files**

- Create: `src/graph_sym_perf/explanation/models.py`
- Create: `src/graph_sym_perf/explanation/collector.py`
- Test: `tests/unit/test_explanation_collector.py`
- Delete: `src/graph_sym_perf/oracle/plan_oracle.py`
- Delete: `src/graph_sym_perf/feedback/__init__.py`
- Delete: `src/graph_sym_perf/feedback/model.py`
- Delete: `src/graph_sym_perf/feedback/scheduler.py`
- Delete: `src/graph_sym_perf/feedback/scoring.py`
- Delete: `src/graph_sym_perf/feedback/state.py`
- Delete: `src/graph_sym_perf/coverage/__init__.py`
- Delete: `src/graph_sym_perf/coverage/model.py`
- Delete: `src/graph_sym_perf/coverage/pmc.py`
- Delete: `src/graph_sym_perf/coverage/report.py`
- Delete: `src/graph_sym_perf/coverage/state.py`
- Modify: `src/graph_sym_perf/app/mining.py`
- Modify: `src/graph_sym_perf/experiment/resume.py`
- Modify: `src/graph_sym_perf/experiment/calibration_health.py`

**Interfaces**

- `collect_explanation(issue: SubmitReadyIssue, adapter) -> ExplanationEvidence`。
- ExplanationEvidence 可以包含 normalized plan、PROFILE operator metrics、db hits、rows、memory 与 page-cache 信息，但没有 `is_candidate`、reward、novelty 或 promotion 方法。

- [ ] **Step 1:** 写失败测试：collector 输入不是 `SubmitReadyIssue` 时拒绝采集；ExplanationEvidence 不能改变 issue stage。
- [ ] **Step 2:** 删除旧 `TransformScheduler`、`FeedbackState`、`PMCCoverageState`、`pmc_new_units` 和 checkpoint 中的 plan-guidance state；保留的新 `AdaptiveRuleSampler` 只能读取 versioned yield snapshot。
- [ ] **Step 3:** 删除候选判定中的 plan changed、profile supported、plan-only tier 和 PMC 排序。
- [ ] **Step 4:** 保留必要的 plan normalization implementation，迁入 explanation module；删除 `plan_oracle.py`。
- [ ] **Step 5:** 更新 config schema，拒绝 `coverage`、`feedback`、`include_plan_only` 和 plan-guidance 字段。
- [ ] **Step 6:** 运行：

```bash
! rg -n "TransformScheduler|FeedbackState|PMCCoverageState|pmc_new_units|include_plan_only|plan_only" src config tests
.venv/bin/python -m pytest tests/unit/test_explanation_collector.py tests/contracts -q
```

### Task 10：建立分级、内容寻址的产物存储

**Files**

- Create: `src/graph_sym_perf/artifacts/schema.py`
- Create: `src/graph_sym_perf/artifacts/policy.py`
- Create: `src/graph_sym_perf/artifacts/store.py`
- Create: `src/graph_sym_perf/issues/evidence.py`
- Create: `issues/README.md`
- Test: `tests/unit/test_artifact_policy.py`
- Test: `tests/unit/issues/test_evidence_package.py`
- Test: `tests/integration/test_artifact_budget.py`
- Modify: `src/graph_sym_perf/core/models.py`
- Modify: `src/graph_sym_perf/reporter/artifacts.py`

- [ ] **Step 1:** minimal 只保存 query hash、rule/site、等价性摘要、screen runtime 和错误分类。
- [ ] **Step 2:** candidate 保存 discovery paired timing、query pair 和 graph snapshot 引用。
- [ ] **Step 3:** validation 保存两个 session 的 raw paired timing、负对照、环境与 fingerprint。
- [ ] **Step 4:** issue 保存 reduced pair、reproducer、最终 validation 和按需 ExplanationEvidence。
- [ ] **Step 5:** 大 plan/profile 内容寻址只存一次；`cases.jsonl` 不嵌套 plan 或 profile samples。
- [ ] **Step 6:** 聚合器流式读取；达到 shard 500 MB budget 时降级非候选诊断并显式告警。
- [ ] **Step 7:** 定义公开 issue 最小证据包：`metadata.yaml`、`setup.cypher`、`original.cypher`、`transformed.cypher`、`reduced_original.cypher`、`reduced_transformed.cypher`、`validation.json`、`README.md`。缺少历史字段时显式写 `unknown` reason，不补造数据。
- [ ] **Step 8:** 为现有 11 个 issue 各建立一个证据包；从本地文件、已提交内容和公开附件恢复可核查材料。用户已删除且无法恢复的本地文件不得通过 checkout 偷偷恢复，需记录缺失并使用公开来源重建。
- [ ] **Step 9:** issue package 记录 DBMS/version、detector/rule/validator/reducer version、discovery/validated runtime、GitHub URL、DeveloperStatus、status evidence URL、last_checked_at 和 `revalidated_by_v3`。
- [ ] **Step 10:** 运行：

```bash
.venv/bin/python -m pytest tests/unit/test_artifact_policy.py tests/unit/issues/test_evidence_package.py tests/integration/test_artifact_budget.py -q
```

验收：1000 accepted-pair shard ≤500 MB，candidate 到 SubmitReadyIssue 的 hash chain 可完整追溯，11 个旧 issue 各有独立 evidence package。

### Task 11：迁移 CLI 并同步文档

**Files**

- Modify: `src/graph_sym_perf/cli/__main__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `scripts/README.md`
- Create: `CONTEXT.md`
- Create: `docs/architecture.md`
- Create: `docs/experiment.md`
- Create: `docs/validation.md`
- Create: `docs/reducer.md`
- Create: `docs/rules.md`
- Create: `docs/dbms-compatibility.md`
- Create: `docs/artifacts.md`
- Create: `docs/operations.md`
- Create: `docs/issue-lifecycle.md`
- Create: `docs/paper-evaluation.md`
- Create: `docs/terminology.md`
- Create: `docs/research-design.md`
- Create: `docs/advisor-presentation-outline.md`
- Create: `docs/adr/0001-runtime-first-oracle.md`
- Create: `docs/adr/0002-independent-issue-validation.md`
- Create: `docs/adr/0003-gdsmith-only-generation.md`
- Create: `docs/adr/0004-remove-plan-guidance.md`
- Delete after migration: `document/1.md`
- Delete after migration: `document/2.md`
- Delete after migration: `document/3.md`
- Delete after migration: `document/4.md`
- Delete after migration: `document/GraphSymPerf.md`
- Delete after migration: `document/GraphSymPerf_experiment_schedule.md`
- Delete after migration: `document/GraphSymPerf_导师汇报PPT大纲.md`
- Delete after migration: `document/GraphSymPerf_导师汇报PPT大纲_v2_论文模式.md`
- Delete after migration: `document/rule_space.md`
- Delete after migration: `document/总体研究主线.md`
- Delete after CLI/worker parity: `src/graph_sym_perf/app/mining.py`
- Delete after CLI/worker parity: `src/graph_sym_perf/app/gdsmith_mining.py`
- Delete after CLI/worker parity: `src/graph_sym_perf/app/__init__.py` if empty
- Delete after replacement: `tests/unit/test_mining_pmc_feedback.py`
- Delete after replacement: `tests/integration/test_gdsmith_neo4j_profile_oracle.py`
- Modify: `tests/unit/test_oracle.py`
- Modify: `tests/unit/test_known_replay_cases.py`
- Modify: `tests/unit/test_preflight.py`
- Modify: `tests/unit/test_run_no_ready.py`
- Test: `tests/integration/test_cli_experiment_lifecycle.py`
- Test: `tests/integration/test_cli_issue_lifecycle.py`

- [ ] **Step 1:** 新 CLI 只调用 planner/supervisor/validator/reducer/exporter 的公开 interface；普通用户只需要 `experiment run` 和 `experiment resume`。
- [ ] **Step 2:** 实现 `graphsymperf experiment run --config config/experiment.yaml`，默认跑完整 pipeline；`doctor/plan/status` 与 `issue validate/reduce/export` 标为 advanced commands。
- [ ] **Step 3:** `issue export` 输入没有 `SUBMIT_READY`、reducer 不是 `REDUCED_VALID` 或 CORE SafetyCertificate 缺失时必须非零退出并列出缺失 gate。
- [ ] **Step 4:** 将 `_preflight` 迁入 `experiment/doctor.py`、GDsmith artifact 写入迁入 generation/artifact 模块、case execution 迁入 worker；对应测试改为公共接口测试后删除 `app/mining.py` 与 `app/gdsmith_mining.py`，消除 runner 对 app 私有函数的反向依赖。
- [ ] **Step 5:** README 只保留 GDsmith main run、resume、结果位置和 issue lifecycle；不出现 LDBC fallback、QPG、plan-only 或多套正式配置。
- [ ] **Step 6:** `docs/terminology.md` 统一 accepted query pair、runtime candidate、validation passed、REDUCED_VALID、SubmitReadyIssue、Reported Issue Report、Developer-Validated Bug、ReliabilityFinding、ExplanationEvidence 等术语。
- [ ] **Step 7:** `docs/paper-evaluation.md` 固定论文第一贡献和 RQ：effectiveness、precision、rule utility、reduction、generality；明确累计 issue corpus 不等同于单次无偏 yield，现有 11 与后续案例逐项说明且按版本归因。
- [ ] **Step 8:** `docs/issue-lifecycle.md` 给出 90 天反馈观察窗、waiting reply 右删失、confirmed/fixed 证据要求，以及 reported/validated 两套数字。
- [ ] **Step 9:** 将 `document/GraphSymPerf.md` 与 `document/总体研究主线.md` 中仍有效的方法定义迁移到 `docs/research-design.md`；删除 PMC/QPG 主判定、LDBC 备选和旧分层 oracle 表述。
- [ ] **Step 10:** 将 `document/rule_space.md` 迁移到 `docs/rules.md`，以 CORE/EXPERIMENTAL/CONTROL/REMOVED 和 SafetyCertificate 取代 PMC-aware 分类。
- [ ] **Step 11:** 将 `document/GraphSymPerf_experiment_schedule.md` 与 `document/2.md` 的有效运维内容迁移到 `docs/experiment.md`、`docs/operations.md` 和 runbook；旧 main/ablation/calibration 多配置流程不保留。
- [ ] **Step 12:** 将两个导师 PPT 大纲合并为 `docs/advisor-presentation-outline.md`，统一第一贡献为 safety-point BOOL/SYM + runtime-first oracle；plan/PMC 只作解释，删除反馈引导页。
- [ ] **Step 13:** `document/1.md`、`3.md`、`4.md` 是旧任务说明/校准修复记录；提取仍需保留的事实后删除，不作为项目规范文档。
- [ ] **Step 14:** README、CONTEXT、docs、config 和 artifact schema 使用同一术语；完成迁移后删除整个旧 `document/` 内容，避免两个事实源。
- [ ] **Step 15:** 运行：

```bash
.venv/bin/python -m pytest tests/integration/test_cli_experiment_lifecycle.py tests/integration/test_cli_issue_lifecycle.py tests/contracts -q
rg -n "plan-only|PROFILE.*runtime|runtime.*PROFILE|QPG|查询计划引导" README.md CONTEXT.md docs src config tests
```

第二条命令的每个剩余匹配都必须是“已删除能力的历史说明”或明确的反例测试。LDBC 负面 schema tests 可以保留字面量，但生产代码、正式配置、CLI 和 README 中不得保留 LDBC 能力。

### Task 12：迁移回归夹具并清理工作区

**Files**

- Create: `tests/fixtures/regressions/manifest.yaml`
- Create: `scripts/audit_cleanup.py`
- Test: `tests/unit/test_cleanup_safety.py`
- Modify: `.gitignore`
- Delete only after evidence migration and approved dry-run: `runs/*`、`logs/*`、旧 results/reports、缓存、旧配置、旧脚本和已迁移的旧 issue 产物

- [ ] **Step 1:** 将 11 个公开 issue 的 evidence package 与 regression fixture 分开：evidence 保留历史事实；regression 只包含能在新 IssueValidator 下执行的最小案例。
- [ ] **Step 2:** 校验每个 evidence package 的文件 hash、GitHub URL、metadata schema 和可用 reproducer；生成 `evidence_inventory.json`。未通过校验前禁止删除任何来源文件。
- [ ] **Step 3:** `audit_cleanup.py` 输出 path、大小、tracked 状态、用户未提交状态、evidence 引用、迁移目标和删除原因；默认 dry-run，任何未提交 tracked path 自动标为 `PROTECTED`。
- [ ] **Step 4:** 清理范围固定为：旧 `runs/`、`logs/`、`results/`、generated reports、summary/triage/PMC/PROFILE、shard checkpoints、GDsmith raw、临时 runtime CSV/JSONL、`__pycache__`、`.pytest_cache`、coverage、build/dist、临时 HTML/PDF/图片和已迁移的重复 issue 目录。
- [ ] **Step 5:** 清单经用户最终确认后执行删除；删除命令必须只消费 `deletion_manifest.json` 中 `APPROVED` 项，不允许重新 glob 扩大范围。
- [ ] **Step 6:** 保留 `.venv`，除非用户明确要求重建；保留 `issues/` 下 11 个最小证据包和新 regression fixtures。
- [ ] **Step 7:** 统一 tracked/ignored 规则，避免目录忽略但局部文件仍 tracked；清理后报告磁盘前后体积和删除文件计数。
- [ ] **Step 8:** 运行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
git status --short
```

### Task 13：最终端到端验收与新实验交接

**Files**

- Create: `tests/integration/test_end_to_end_acceptance.py`
- Create: `docs/runbook-main-experiment.md`
- Modify: `README.md`

**Interfaces**

- Consumes: `config/experiment.yaml`、七个 GDBMS environment、GDsmith fixed commit 和已完成的 Task 1–12。
- Produces: 可审计的 preflight report、112-task `resolved_manifest.json` 和用户重新运行实验的唯一命令；本任务不复用任何旧 run artifact。

- [ ] **Step 1:** 写端到端失败测试：fake 七 DBMS 下，每个 shard 只有达到目标 accepted-pair count 才能 COMPLETED；999 必须 INCOMPLETE，单 shard 卡死不影响其他 shard。
- [ ] **Step 2:** 运行全部 unit、contract、property 和 integration tests；任何 skip 必须列出原因，不能把缺少真实 DBMS 的测试写成 pass。
- [ ] **Step 3:** 运行 `graphsymperf experiment doctor --config config/experiment.yaml`，核对七个 DBMS、driver、GDsmith commit、graph isolation、磁盘预算和环境变量；缺少任一真实依赖时停止在 doctor，不启动部分正式矩阵。
- [ ] **Step 4:** 运行 `graphsymperf experiment plan --config config/experiment.yaml`，断言 7 systems、112 shards、112,000 accepted-pair budget、112 个唯一 shard seeds。
- [ ] **Step 5:** 按第 4.6 节依次执行每 DBMS 1×10、1×100、1×1000；只有每级 gate 都通过，才交付正式命令。
- [ ] **Step 6:** 在 `docs/runbook-main-experiment.md` 写出唯一正式命令、resume/status、INCOMPLETE 处理、validation/reducer 产物位置和 issue evidence 路径：

```bash
graphsymperf experiment run --config config/experiment.yaml
```

- [ ] **Step 7:** 不在重构提交中自动启动 7×16×1000 长跑；由用户在确认 DBMS 版本、资源和清理结果后执行上述命令。新运行只能写入全新 run-id，不得读取旧 runs/logs。
- [ ] **Step 8:** 最终验证：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
graphsymperf experiment plan --config config/experiment.yaml
git diff --check
git status --short
```

---

## 4. 独立 issue validation 协议

### 4.1 为什么必须是独立门禁

discovery 阶段从大量 query pair 中挑最大 ratio，天然存在 winner's curse 和回归均值风险。同一批样本上“再次计算一次 median”不算复核。validation 必须使用从未参与筛选的新样本，并重新建立图状态和连接。

### 4.2 原始 pair validation

每个通过 discovery screen 的 candidate 执行：

1. 验证 query hash、parameters hash、GDsmith graph artifact hash、DBMS version/config hash。
2. 新 OS 进程启动 adapter，reset 后加载 graph snapshot，并核对 node/edge/label/type/property count 与 fingerprint。
3. purity gate 拒绝 write、volatile/current-time/internal-ID function、未固定参数、无确定性排序的 LIMIT/SKIP、无稳定输入顺序的 collect 和无法完整比较的结果。
4. 执行完整 typed result equivalence：有 ORDER BY 比较 sequence，无 ORDER BY 比较 multiset；重复行、NULL/missing、类型、list/map 顺序规则和 graph entity 内容必须保持。超过 100,000 rows 或 driver 截断时记 `inconclusive`，不能 pass。
5. 运行 A-vs-A、B-vs-B 负对照；样本进入目标 DBMS 的 A/A noise store。结果不稳定、出现 health event 或绝对差异常超出当前噪声模型时，该 session 作废。
6. 5 次 warmup 后执行 20 个 balanced randomized pairs，保证 AB/BA 各 10 次。
7. 关闭进程和连接，重新加载同一 graph snapshot，执行第二个独立 session。
8. 两个 session 同方向，合并 median ratio ≥3.0、95% bootstrap CI lower ≥2.0、median absolute delta > A/A noise p99，且没有 infrastructure event，才进入 `VALIDATION_PASSED`。

### 4.3 magnitude 复核

- `discovery_ratio` 只作为历史字段，不进入开发者标题。
- `validated_ratio` 使用两个 validation session 中更保守的 paired-median ratio。
- `magnitude_stability=stable`：validation 与 discovery 同方向，且 validation ratio ≥ discovery ratio 的 50%。
- `magnitude_stability=shrunk_but_real`：缩小超过 50%，但 validation 仍独立达到 issue-grade 门槛；可以继续，但只能报告 validated ratio。
- `magnitude_stability=unstable`：方向变化、session 间差异过大或仅一个 session 通过；拒绝自动提交。

不把 50% retention 作为真实性的唯一判据。例如 discovery 100×、validation 10× 仍可能是真问题，但 issue 只能写 10×，不能继续写 100×。

### 4.4 reduced pair validation

原始 pair 通过后才启动 reducer。reducer 输出必须重新执行完整 4.2 协议：

- reduced pair 不等价：reduction rejected。
- reduced runtime 不达标：`REDUCTION_INVALIDATED`，保留原始 validated pair 供人工研究，但不自动 export。
- 达到 reducer 预算但未达局部 1-minimal：`REDUCTION_PARTIAL`，必须人工审核。
- reduced pair 达到局部 1-minimal 并通过：`REDUCED_VALID`。
- 只有 `REDUCED_VALID`、CORE SafetyCertificate 完整且无 possible duplicate 时，`promotion.py` 才自动构造 `SubmitReadyIssue`。

### 4.5 timeout candidate

query timeout 不自动等于性能 bug。只有当两个 validation session 中：

- 一侧至少 80% 次数在 deadline 内完成；
- 另一侧至少 80% 次数稳定 timeout；
- 完成侧的 conservative upper runtime 能给出超过 issue threshold 的 lower-bound ratio；
- DBMS process 没有 crash、OOM、连接断开或 graph state 变化；

才允许记为 `one_sided_timeout_supported`。否则是 environment/availability failure。

DBMS crash、worker hang、driver 无法取消、OOM 或连接断开不走上述 timeout 分支，而是形成 `ReliabilityFinding`。即使能够稳定复现，也不进入 30+ runtime issue reports。

### 4.6 正式实验递进门禁

不得从单元测试直接跳到 7×16×1000：

1. **纯离线：** planner、runtime statistics、rule proofs、validation promotion、reducer dependency、artifact policy 和 cleanup safety 全部通过。
2. **故障注入：** 永久阻塞 query、GDsmith crash、DBMS restart、graph fingerprint mismatch、磁盘预算、worker SIGKILL；supervisor 必须继续并可 resume。
3. **每 DBMS 1×10：** 只用 GDsmith，验证生成、加载、10 accepted pairs、等价性、runtime、validation、reducer 和 artifact hash chain。
4. **每 DBMS 1×100：** 估计 accepted rate、rule quarantine rate、A/A noise floor、validation rejection rate、timeout 和产物体积；结果写入版本化 manifest，但不要求冻结整个后续 campaign。
5. **每 DBMS 1×1000：** 验证 exact accepted-pair count、长期连接、恢复、独立 validation 和 500 MB artifact budget。
6. **正式 7×16×1000：** 只有前五级全部通过才启动。

正式验收必须同时满足：

- planner 精确输出 112 shards 和 112,000 accepted-pair budget，seed/accepted-pair AST hash 不跨 shard 重复。
- 每个 completed shard 恰好 1000 accepted pair records；999 必须是显式 `INCOMPLETE`。
- 任一 shard 卡死、DBMS 退出或 GDsmith 失败不导致整轮静默退出。
- 任何 issue draft 都能追溯到两个 original-pair validation sessions、reducer trace 和 reduced-pair validation。
- 不存在 profile-only、plan-only、PMC-only reportable item。
- 1000 accepted-pair shard ≤500 MB；文档、CLI、schema、artifact 使用同一状态术语。
- 清理后不保留旧 runs/logs；现有 11 个 issue 保留最小 evidence package，能在新 validator 下运行的子集另存 regression fixture。

## 5. 代码层面误报防线

| 误报来源 | 当前风险 | 最终版强制修复 |
|---|---|---|
| PROFILE 当 runtime | triage validation 调 `adapter.profile` | RuntimeHarness interface 只接收 `execute` executor |
| profile-only 被发布 | issue tracker 接受 profile-supported | exporter 只接收 `SubmitReadyIssue` |
| 截断结果误判等价 | adapter max_rows 与结果上限语义不统一 | `complete_result` 必须为 true，否则 inconclusive |
| 类型/重复数被抹平 | row dict/string 比较可能忽略类型或 bag multiplicity | typed canonical value + ordered sequence/unordered multiset 完整比较 |
| 非确定查询 | 当前缺统一 volatile function gate | AST purity + per-dialect volatile registry + A/A result stability |
| 无序 LIMIT/SKIP | 两次执行可能访问不同子集 | 缺少确定性 ORDER BY 时直接拒绝 |
| 聚合顺序漂移 | collect 等结果依赖未定义输入顺序 | 无稳定排序证明时拒绝自动 issue |
| graph state 漂移 | GDsmith backend/adapter graph identity 可能不同 | snapshot hash + reset/load + count/fingerprint |
| stale connection | GDsmith 后复用旧 adapter | generation 后关闭，执行/validation 新建连接 |
| 假硬超时 | thread future timeout 无法杀死阻塞驱动 | OS process group deadline |
| 顺序/缓存偏差 | discovery 固定 fast 后 slow 或 PROFILE 顺序 | balanced randomized paired execute |
| 环境噪声 | 无 A/A、B/B control gate | 每 session 负对照 + MAD gate |
| winner's curse | 在筛选样本上复用 ratio | 两个 fresh validation sessions |
| 状态字段混淆 | `support_level` 同时承载 plan/profile/runtime | `PipelineStage` + `DeveloperStatus` + `RuntimeVerdict` 枚举 |
| reducer 保错症状 | runtime/profile ratio 取最大值 | SymptomPredicate 只看普通 execute runtime |
| rule soundness 漂移 | strict rule 动态 mismatch 只扣 reward | 首次 mismatch 自动 quarantine 并保存 counterexample |
| expected work inflation | 重复昂贵 expr 可自然变慢 | stress/control rule 不具备 submit eligibility |
| 多重比较 | 数千 pair 中挑极值 | discovery/validation 分样本；报告 validation 保守值 |
| 重复 issue | 聚类依赖 plan/PMC | validation 后按 DBMS/version/rule/reduced AST/runtime direction 去重 |
| 方言有损兼容 | 当前 regex filter 或首标签折叠可能改变语义 | CompatibilityDecision + proof-backed rewrite；其余 REJECT/top-up |
| crash/hang 混入性能 | query/driver/DBMS timeout 状态混杂 | ReliabilityFinding 与 runtime report 两条独立状态链 |

额外约束：

- 所有 error reason 使用枚举，不用字符串包含判断决定 promotion。
- artifact schema 记录测量协议版本；旧协议 artifact 不能被新 exporter 自动读取为 submit-ready。
- DBMS version、driver version、container image digest、CPU architecture 和关键配置缺失时 validation 不得 pass。
- 同一 DBMS 没有独立 graph slot 时，validation 期间暂停该 DBMS 的其他 shard。
- 对发现速度进行优化不能降低 validation 门槛；screen 可以宽松，export 只能更严格。
- adaptive rule scheduler 只能读取 accepted 与 validation-passed 计数；任何 explanation/developer outcome 泄漏都视为契约失败。
- 旧 issue 若未按 v3 protocol 复核，只能标记其历史证据和发现版本，不能自动写 `revalidated_by_v3=true`。

## 6. BOOL/SYM 规则审查与调整

### 6.1 当前统计与公开 issue 反馈的使用边界

当前 Neo4j shard 的 `rule_stats.json` 显示，`and_expr_or_true`、`and_not_false_and`、`or_expr_null_contradiction`、`and_not_expr_null_contradiction` 和 `and_multi_layer_redundancy` 各获得约 758–766 次尝试，而多数 SYM 规则只有几十次，部分生成数为 0。这证明旧 feedback 发生集中效应；由于 candidate 主要还是 PROFILE 层信号，不能据此声称 NULL 规则真实 runtime 命中率更高。

现有 11 个公开报告中，3 个 pattern/MATCH 顺序类 SYM 案例均得到用户记录的正向结果（2 fixed、1 confirmed），BOOL 案例反馈更混合。该现象只用于提高 SYM 规则实现与 safety-site 覆盖的优先级，不能作为“SYM 必然优于 BOOL”的统计结论；DBMS、维护者活跃度和 issue 写法是混杂因素。

### 6.2 BOOL 既有规则裁决

| 当前规则 | 最终处置 | 安全层级 | 理由 |
|---|---|---|---|
| `and_true_or` + `and_expr_or_true` | 合并 | `constant_true_disjunction(position)`，CORE | 同一恒真结构的操作数方向参数；插入表达式必须 pure、total、低成本，避免人为重复昂贵工作 |
| `or_false_and` + `or_expr_and_false` | 合并 | `constant_false_conjunction(position)`，CORE | 同上 |
| `and_expr_null_partition` | 保留 | `null_partition_tautology`，CORE | 三值逻辑完整分区，必须附 truth-table proof |
| `or_expr_null_contradiction` | 保留 | `null_contradiction`，CORE | 语义可证明；开发者反馈混合，由 validated yield 自然调整预算 |
| `and_not_expr_null_contradiction` | 删除 | — | 与 null contradiction/partition 高度重叠，多一层 NOT 不构成独立语义空间 |
| `and_excluded_middle_or_null` | 保留 | `null_complete_excluded_middle`，CORE | 只有 null-complete 形式可进入 CORE；普通 `P OR NOT P` 在 P=NULL 时不安全 |
| `and_not_false_and` | 删除 | — | 被 constant-false family 覆盖 |
| `and_double_negated_true_or` | 删除 | — | 把多个已有恒真模板堆叠，难以形成清晰 issue 归因 |
| `and_null_guarded_true` | 删除 | — | 被 constant-true family 覆盖 |
| `and_case_true` | 移出自动链 | EXPERIMENTAL | DBMS 真实求值 expression 时变慢可能是预期成本 |
| `or_guarded_contradiction` | 移出自动链 | EXPERIMENTAL | 重复求值 expression，易被开发者视为构造性开销 |
| `or_duplicate_predicate` | 保留为负对照 | CONTROL | 用于测量 dedup/noise，不自动生成 issue |
| `and_distributive_absorption` | 删除 | — | 与 null-complete、多层 redundancy 重叠且重复工作难归因 |
| `and_multi_layer_redundancy` | 保留 pilot | EXPERIMENTAL | 可用于研究放大，但未证明不是预期执行开销前不得自动提交 |
| 四个 `arcade_*` | 删除研究身份 | dialect rendering | 它们是方言降级模板，不是四条独立规则 |

### 6.3 BOOL 新规则候选与严审

| 新规则 | 判决 | 进入主实验的前置条件 |
|---|---|---|
| `double_negation_predicate`：`P <-> NOT NOT P` | 升级候选 | P 为 pure boolean AST；三值 truth table、各 dialect contract、动态 mismatch=0 后进入 CORE |
| `de_morgan_predicate`：`NOT(P OR Q) <-> NOT P AND NOT Q`，以及 AND 对偶 | 升级候选 | P/Q pure、total、无 pattern predicate 或 scope change；完整三值证明后进入 CORE |
| `numeric_equality_range`：`x = c <-> x >= c AND x <= c` | 保留实验 | EXPERIMENTAL；需证明同型有限数值并排除 NaN、字符串和隐式 cast，且两次比较带来的预期开销可能削弱 issue 价值 |
| predicate factorization/defactorization | 暂不采用 | 两侧重复求值次数不同、开发者易归因为预期成本，且跨 dialect 三值证明/类型约束成本高 |
| `IN` 展开为 OR | 暂不采用 | Cypher `IN` 的 NULL/list 语义与方言差异大，误报风险高于预期收益 |

新增不追求数量。候选规则若不能通过 proof、dialect contract 和独立 runtime pilot，就不能在 `rules.yaml` 中升级为 CORE。

### 6.4 SYM 既有规则裁决

| 当前规则 | 最终处置 | 理由 |
|---|---|---|
| `pattern_order_symmetry` | CORE；优先完善所有安全 site | 只排列同一个 MATCH graph pattern 内的 constituent path patterns，不改变 clause 边界与 relationship uniqueness scope |
| `predicate_order_symmetry` | CORE | 只允许 pure、total conjunct；枚举所有 WHERE subtree |
| `node_order_symmetry` | CORE | 修复只看第一个 atom导致生成率为 0 的问题，保持同一 graph pattern |
| `path_endpoint_symmetry` | EXPERIMENTAL，证明后升级 | path/relationship list variable 必须不可观察，长度边界和 multiplicity proof 完整 |
| `pattern_decomposition_symmetry` / `pattern_fusion_symmetry` | EXPERIMENTAL | 单 graph pattern 与多个 MATCH 的 relationship uniqueness scope 可能不同，默认禁止自动 issue |
| `edge_notation_symmetry` | CONTROL | 通常是 parser normalization，适合作为 noise control |
| `label_order_symmetry` | capability-gated CORE/CONTROL | 只在支持多标签且语义证明完整的系统启用；AgensGraph single-label 下 REJECT |
| `property_map_order_symmetry` | 删除主实验 | 当前生成率为 0，且预期只改变表面序列化 |
| `alpha_renaming_symmetry` | CONTROL | 不期望产生真实 runtime 差异，适合噪声校准 |
| `exploratory_pattern_order_symmetry` / `exploratory_predicate_order_symmetry` | 删除 | strict 版本完善后重复 |
| `exploratory_optional_match_order_symmetry` | 删除自动 issue 链 | null introduction 语义风险过高 |
| `exploratory_with_filter_shift_symmetry` | quarantine | 完成 scope/null/aggregation proof 后才能以新 strict rule 回归 |
| `exploratory_projection_reorder_symmetry` | 删除 | 输出列次序可能属于 observable schema，当前 row-dict 比较会掩盖差异 |

### 6.5 SYM 新规则候选与严审

| 新规则 | 判决 | 安全前提 |
|---|---|---|
| `property_map_where_symmetry`：`MATCH (n {k:v})` 与 `MATCH (n) WHERE n.k=v` | EXPERIMENTAL | ordinary MATCH、单个静态 key、pure literal/parameter、无 OPTIONAL/scope barrier；跨 dialect proof 完成后升级 |
| `label_where_symmetry`：pattern label 与 WHERE label predicate 互换 | EXPERIMENTAL | dialect capability 明确支持 label predicate，单标签起步 |
| `single_hop_varlength_symmetry`：隐藏变量下 `[:T*1..1]` 与 `[:T]` | EXPERIMENTAL | path value、relationship list 和 relationship variable 均不可观察；multiplicity proof 通过后升级 |
| `independent_mandatory_match_reorder` | CORE 候选 | 只交换连续 mandatory MATCH；变量依赖图证明独立；WHERE 保持附着；不跨 WITH/OPTIONAL/ORDER/LIMIT/aggregation/subquery barrier |

### 6.6 探索下限 + validated runtime yield

- `rules.yaml` 明确每个 rule 的 tier、site class、per-shard exploration floor 和 proof version。
- 每条 CORE rule 先获得至少 20 个 accepted pairs 的探索机会；规则在目标方言不适用时记录 `RULE_NOT_APPLICABLE`，不做有损兼容。
- 探索下限之后，每 100 accepted pairs 更新一次权重，唯一收益信号是 Laplace-smoothed validated runtime yield。
- plan、PROFILE、PMC、discovery maximum、confirmed/fixed 或维护者响应速度不得进入调度输入。
- CONTROL 必须保留，用于测量 equivalence/runtime false-positive floor；EXPERIMENTAL 只供人工研究。
- 方法可以在不同 run 之间演进；每个 resolved manifest 保存当时的规则权重、yield snapshot 与版本，保证 issue 归因而不是假装全程冻结。

### 6.7 图规模与放大

- 小图只用于语义、方言和故障门禁。
- 128/512/2000 级别作为 calibration 阶梯，选择让绝对 runtime 超过噪声但不频繁 timeout 的规模。
- 高 ratio、低 absolute delta 候选可受控放大；原始与放大条件分开报告。
- 不允许事后只挑最有利规模；validation 使用当前 run resolved manifest 中记录的规模策略。

---

## 7. 顶会实现借鉴与边界

- **AMOEBA（SIGMOD 2022）**：借鉴等价 query pair、重复 runtime validator、mutation/validator feedback 和人工提交；本项目把反馈严格限定为 validated runtime yield，不采用 plan-cost 或 PROFILE 作为 issue 门禁。
- **GDsmith（ISSTA 2023）**：继续利用语义有效、非空结果和行为多样性生成；它解决的是图查询有效性，不等于运行可靠性或 runtime 显著性。
- **QPG（ICSE 2023）**：明确不采用。其核心是用新 query plan 反馈引导生成；当前 BOOL/SYM 规则空间有限，新增复杂状态与产物成本高于预期收益。
- **SQLancer reducer**：借鉴 statement/AST delta debugging、每次删除后重新应用 oracle、报告前在最新 DBMS 版本复现；不照搬 SQL AST。
- **PQS/SQLancer（OSDI 2020）**：借鉴公开区分 fixed/verified/intended/duplicate，以及在持续 campaign 中切换版本、避开已知 bug 的版本归因纪律；不要求把整个发现过程伪装成冻结实验。
- **PINOLO（USENIX ATC 2023）**：借鉴把大量 anomalous pairs 去重为少量 unique reports、公开 waiting 状态和固定预算对比；GraphSymPerf 同样分开报告 raw candidate、validated pair 和 submitted report。
- **SQLess（ISSTA 2024）**：借鉴 dependency-aware、semantics-sensitive AST trimming，避免删除定义后仍保留使用点；为 Cypher 自行实现 scope/variable/path dependency。
- **CERT（ICSE 2024）**：只借鉴重复确认和报告前复现纪律；不引入 cardinality/plan oracle。
- **Kalibera & Jones（2013）**：借鉴效应量置信区间与不确定性报告。
- **The Ordering Trap（USENIX ATC 2023）**：用于约束成对交错和顺序随机化，避免 benchmark ordering bias。

Primary sources:

- AMOEBA: https://doi.org/10.1145/3510003.3510093
- PQS: https://www.usenix.org/conference/osdi20/presentation/rigger
- PINOLO: https://www.usenix.org/conference/atc23/presentation/hao
- GDsmith: https://doi.org/10.1145/3597926.3598125
- QPG: https://doi.org/10.1109/ICSE48619.2023.00119
- CERT: https://doi.org/10.1145/3597503.3639128
- SQLess: https://doi.org/10.1145/3650212.3680317
- SQLancer: https://github.com/sqlancer/sqlancer
- Apache AGE: https://github.com/apache/age
- AgensGraph manual: https://tech.skaiworldwide.com/docs/en/agensgraph/13/developer_manual/installation.html

---

## 8. 论文贡献、研究问题与统计口径

### 8.1 第一贡献与支撑贡献

第一贡献固定表述为：

> GraphSymPerf 提出一种面向图数据库的 safety-point 驱动语义保持变换方法，使用带 `SafetyCertificate` 的 BOOL/SYM 规则构造等价查询对，并以噪声校准、独立复核的 runtime-first oracle 检测性能异常。

支撑贡献：

1. 两个独立 validation session 与 typed full-result equivalence，降低 runtime 误报。
2. paired AST reducer，在保持等价性与 issue-grade runtime gap 的同时最小化查询。
3. capability-aware dialect compilation，统一测试七个 GDBMS，并诚实报告 AGE proxy 与 AgensGraph single-label 边界。
4. supervisor/checkpoint/recovery，使 7×16×1000 accepted-pair campaign 可恢复执行。
5. 累计 30+ 个经过查询级去重的 `Reported Issue Reports`，并单独报告 unique bug families、Developer-Validated Bugs、fixed、uninterested、duplicate 和 waiting reply。

### 8.2 Research Questions

- **RQ1 Effectiveness：** GraphSymPerf 能发现多少 validated、reduced、deduplicated、submit-ready issue reports？每万 accepted pairs 和每 CPU-hour 的 yield 是多少？
- **RQ2 Precision：** screen 候选中有多少通过原始 validation、reducer 和 reduced validation？主要拒绝原因是什么？
- **RQ3 Rule Utility：** BOOL、SYM 以及每条 CORE rule 的 applicability、accepted count、validated runtime yield 和 unique report yield 如何？
- **RQ4 Reduction：** reducer 对字符数、AST node 数、clause 数、setup statements 和复现时间的缩减幅度是多少？多少 candidate 达到 REDUCED_VALID？
- **RQ5 Generality：** 七个 GDBMS 的 dialect acceptance、INCOMPLETE rate、validated yield 和 issue distribution 如何？

### 8.3 必报指标

```text
generated_seeds
dialect_accepted
rule_applicable
equivalence_passed
accepted_query_pairs
runtime_candidates
validation_passed
reduced_valid
submit_ready
submitted_reports
developer_confirmed
developer_fixed
duplicate / intended_behavior / uninterested / waiting_reply
```

- `validation precision` 只能描述为 `validation_passed / screened_candidates` 或各门禁 survival rate，不能冒充维护者确认率。
- `maintainer positive rate` 单列为 `(confirmed + fixed) / responded_reports`，waiting reply 不进分母，并同时报告 90 天观察窗和右删失数量。
- 30+ 是累计 submitted/submit-ready corpus 目标；每个 run 的 yield 使用该 run 的 immutable manifest 计算，不能把不同版本的 denominator 混在一起。
- 若一个 DBMS 贡献超过全部 reports 的 40%，必须补充移除该 DBMS 后的敏感性分析；不删除有效 issue，也不设置人为产出上限。
- 同时报告 issue reports 与 unique bug families；同一 DBMS 内仅常量、变量名、标签名变化的近重复只能提交一次。

### 8.4 现有 11 个 issue 的归因

- 11 个均作为独立 `Reported Issue Reports` 逐项说明，保留用户记录的 4 fixed、2 confirmed、2 uninterested、3 waiting reply 快照，并以 GitHub evidence 更新状态。
- 每个 issue 记录发现时 detector/rule/validator/reducer/DBMS version；未通过 v3 复核的不能写成由 v3 validator 发现或验证。
- 旧 issue 与后续 issue 可以共同支撑系统长期有效性，但定量 per-run yield 必须按各自 run/version 分开。
- 不设“冻结 evaluation set”；任何方法演进都通过 versioned registry 和 immutable run manifest 留痕。

### 8.5 对比实验与主张边界

- AMOEBA、CERT、PQS/PINOLO 面向 SQL/关系型 DBMS，不能未经实现和适配就伪装成可直接运行的 Cypher baseline。论文应明确“无直接同任务开源 baseline”，以相关工作能力对比 + 内部受控比较补足证据。
- 若论文声称 validated-yield scheduler 优于均匀调度，必须从同一个 `config/experiment.yaml` 动态派生两个 immutable manifests：`uniform_core` 与 `validated_yield`，使用相同 DBMS、graph、shard seeds、accepted-pair budget 和 runtime policy。派生 manifest 写入 run 目录，不新增永久配置文件。
- 若没有完成上述同预算比较，只能把 yield scheduler 描述为实现策略，不能声称它显著提高发现效率。
- BOOL 与 SYM 的贡献优先使用同一正式 campaign 的 rule-level accepted/validated/report yield 分层统计；如执行额外 family-only run，同样通过动态派生 manifest，而不是恢复旧 ablation YAML。
- validation 的作用用 screen → original validation → reducer → reduced validation 的 survival funnel 和拒绝原因衡量；禁止把未验证候选提交给开发者做“no-validation baseline”。
- reducer 的作用在同一 candidate 上比较缩减前后字符数、AST node、clause、setup statements、复现时间和 validated runtime gap，不需要另造查询 workload。

---

## 9. 主要风险与止损条件

1. **研究口径漂移：** 若代码仍要求 plan/profile 信号才能产生 runtime candidate，立即停止正式实验。
2. **方言代理污染：** AGE/AgensGraph 代理过滤率高时，只能报告 portable-subset 覆盖，不能声称覆盖完整 Cypher。
3. **并发污染：** 未配置独立图实例却并行同 DBMS shards 时，结果无效。
4. **产物再膨胀：** 1×1000 超过 500 MB 时，不进入 16-shard 正式运行。
5. **假硬超时：** 故障注入无法在 deadline 后回收 worker 时，不进入真实长跑。
6. **统计择优：** 单个 run 的阈值、规模、规则版本和初始 yield snapshot 必须写入 immutable manifest；可以启动新版本 run，但不得覆盖旧 manifest 或混合 denominator 来提高结果。
7. **清理误删：** cleanup dry-run 未区分 tracked 文件、用户未提交文件和已迁移 fixture 时，禁止执行删除。
8. **validation 不独立：** 若复用 discovery samples、原连接或未重载 graph snapshot，结果不得标记 validation passed。
9. **reducer 伪最小化：** reduced pair 未重新通过完整 validation 时，不得替换 issue 查询。
10. **规则证明不充分：** strict rule 出现一次无法解释的动态等价 mismatch，立即 quarantine，不以“多数时候等价”放行。
11. **LDBC 残留：** 配置 schema、CLI 或 adapter interface 仍暴露 LDBC 时，不进入正式实验。
12. **plan guidance 回流：** rule selection、candidate ranking 或 promotion 再次读取 plan/PMC state 时，停止正式 run。
13. **误把 report 当 bug：** submitted、waiting、uninterested 被合并宣传为 confirmed bugs 时，停止论文统计并修正状态表。
14. **accepted 口径漂移：** 将 generated seed、dialect accepted 或 equivalence passed 写成 accepted query pair 时，当前 run 结果无效。
15. **有损方言重写：** 为提高 AGE/AgensGraph accepted rate 删除标签、子句或改变 DISTINCT/ORDER BY 时，立即 quarantine compiler 版本。

---

## 10. 推荐提交切分

每个提交只完成一个可回滚目标：

1. `test: capture current experiment contracts`
2. `refactor: add versioned experiment spec and deterministic planner`
3. `feat: add execute-only paired runtime harness`
4. `feat: add independent issue validation and typed promotion gate`
5. `refactor: replace regex reducer with runtime-preserving AST ddmin`
6. `refactor: isolate shard workers behind supervisor`
7. `refactor: make gdsmith the only generator and remove ldbc`
8. `refactor: add safety-certified bool and symmetry rules with validated-yield scheduling`
9. `refactor: remove plan guidance and add passive explanation evidence`
10. `refactor: add tiered content-addressed artifacts`
11. `refactor: unify experiment and issue CLI`
12. `docs: align validation reducer rules and operations`
13. `chore: preserve issue evidence and remove approved historical artifacts`
14. `test: verify end-to-end experiment lifecycle and publish runbook`

任何提交都不得夹带当前用户工作树中不属于该任务的修改。

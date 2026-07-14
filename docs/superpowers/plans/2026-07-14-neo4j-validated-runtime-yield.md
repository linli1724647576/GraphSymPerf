# Neo4j Validated Runtime Yield Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 RuleRegistry 驱动的 BOOL/SYM 独立预算、128/256/512 分阶段图规模、全量紧凑 runtime 记录、两阶段筛选、双独立复验、归约去重和 `validated_runtime_yield` 报告接入 Neo4j 正式 worker，使 fresh P2 查询对能够以可审计口径形成 submit-ready unique issue family。

**Architecture:** 保留现有 schema-v2 历史运行读取能力，新增 schema-v3 validated-yield campaign；正式 worker 只消费不可变 `RulePlanEntry`，Registry 是规则安全事实的唯一来源。P0、P1、P2 各自生成不可变 phase manifest，P1 结束后由确定性 selector 生成 `pilot_selection.json`，P2 只运行入选的“规则 × 图规模”单元；runtime、validation、reduction、dedup 和 yield 各由独立模块承载，worker 只编排生命周期。

**Tech Stack:** Python 3.11、pytest 8、PyYAML、Neo4j Python driver、GDsmith Java subprocess、dataclass/Enum 类型契约、普通 `execute` 配对测量、10000 次 bootstrap、JSON/JSONL/Markdown 制品。

## Global Constraints

- 设计基线为 `docs/superpowers/specs/2026-07-14-neo4j-validated-runtime-yield-design.md`；实现不得降低其中任何安全、复验、归约或置信区间门槛。
- 未加限定的 `validated_runtime_yield` 指 `1000 * fresh REDUCED_VALID unique issue families / CORE discovery accepted pairs`；`validation_pair_yield_per_1k` 仅作诊断指标。
- discovery 只运行 Registry 中证明完整、`tier=CORE` 且 `submit_eligible=true` 的规则；CONTROL 固定 `submit_eligible=false`；EXPERIMENTAL 不进入本 campaign。
- 规范 BOOL ID 固定为 `constant_true_disjunction`、`constant_false_conjunction`、`null_partition_tautology`、`null_contradiction`、`null_complete_excluded_middle`；规范 SYM ID 固定为 `pattern_order_symmetry`、`predicate_order_symmetry`、`node_order_symmetry`、`label_order_symmetry`。
- `and_excluded_middle_or_null` 仅为 `null_complete_excluded_middle` 的历史别名，不得形成第二份 RulePlan 预算。
- `pattern_order_symmetry` 只允许重排同一个普通 MATCH clause 内的顶层逗号 pattern list；不得跨 MATCH、OPTIONAL MATCH、WITH、聚合或变量作用域边界。
- P0：每条 active CORE 在 scale 128 上 50 accepted pairs；另跑两个 CONTROL 的 128×50 接线单元和已知 BUG001 regression。P0 CONTROL 不计入设计中的 P1 `2×3×50=300` 诊断预算。
- P1：每个 active CORE × 128/256/512 使用一个 fresh shard seed、200 accepted pairs；两个 CONTROL × 三个规模各 50 accepted pairs。
- P2：最多 6 个入选 rule-scale cell，每 cell 固定 3 个 fresh shards × 1000 accepted pairs；P1/P2 的 GDsmith seed、图、query pair 和 measurement schedule seed 不重叠。
- 每个 RulePlanEntry 的生成尝试上限固定为 accepted quota 的 10 倍；墙钟上限固定为 P0=3600 秒、P1=7200 秒、P2=21600 秒。触发上限即 `UNDERFILLED` 或 `RESOURCE_EXHAUSTED`，不得转移预算。
- 单 query timeout 从 `config/systems.yaml` 解析并冻结到 RulePlan；运行时配置不能覆盖 Registry tier、submit eligibility、proof version、site class 或 capability gate。
- Stage-1 固定 2 warmups + 3 randomized AB/BA pairs；Stage-2 固定 3 warmups + 12 balanced AB/BA pairs；independent validation 固定每 session 5 warmups + 20 balanced pairs、两个独立 Python 进程和 10000 次 bootstrap。
- 最终 relative path 与 strong-absolute path 使用设计规格第 9 节原值，任一路径通过后仍必须完成 reducer、reduced validation、known-regression exclusion 和 family dedup。
- PROFILE、plan、PMC 和开发者状态不能参与规则选择、Stage-1/2、validation、reducer symptom predicate 或 P2 promotion。
- 当前工作树存在用户未提交修改。执行前必须先由用户决定将这些修改提交为基线还是移植到隔离 worktree；本计划不得 reset、checkout、覆盖或顺带提交它们。
- 当前基线命令 `.venv/bin/python -m pytest tests/unit tests/property tests/contracts -q` 为 `131 passed, 2 failed`；既有失败仅限 `tests/unit/test_agensgraph_compat_builder.py::test_graph_replay_rejects_multi_label_nodes_instead_of_dropping_a_label` 的两个参数实例。本计划不得修改该测试或 AgensGraph/Apache AGE adapter；最终全套测试允许保留这两个基线失败，但不得新增失败。
- 每个任务只暂存该任务列出的文件，并在测试通过后独立提交；禁止使用 `git add .`。

## File Structure

### 新建文件

- `src/graph_sym_perf/runtime/records.py`：紧凑 runtime record 与 fail-closed JSONL store。
- `src/graph_sym_perf/runtime/screening.py`：Stage-1 三队列 Top-K、Stage-2 宽门槛与 timeout queue。
- `src/graph_sym_perf/workload/rule_pairs.py`：按单个 RulePlanEntry 生成 proved/certified query pair。
- `src/graph_sym_perf/experiment/selection.py`：P1 hard gate、P2 cell 排名与 `pilot_selection.json`。
- `src/graph_sym_perf/experiment/campaign.py`：P0→P1→selection→P2 的 phase orchestration。
- `src/graph_sym_perf/validation/session_runner.py`：独立 Python 进程、重连、重载图的 production validation runner。
- `src/graph_sym_perf/validation/regression.py`：已知 regression manifest 与隔离实例 gate。
- `src/graph_sym_perf/issues/dedup.py`：exact/structural fingerprint 和 conservative family merge。
- `src/graph_sym_perf/reporter/yield_report.py`：两种 yield、Wilson CI、blocked bootstrap 和报告输出。
- `config/neo4j_validated_runtime_yield.yaml`：唯一的新 Neo4j campaign 配置。
- `tests/fixtures/regressions/neo4j_bug001/{fast_query.cypher,slow_query.cypher,manifest.yaml}`：不含 PROFILE 的已知 regression 查询与环境契约；图继续引用现有 BUG001 reproducer。
- 对应 unit/integration tests：`tests/unit/runtime/test_records.py`、`tests/unit/runtime/test_screening.py`、`tests/unit/test_experiment_selection.py`、`tests/unit/issues/test_dedup.py`、`tests/unit/reporter/test_yield_report.py`、`tests/integration/test_validated_yield_campaign.py`、`tests/integration/test_regression_gate.py`。

### 修改文件

- `src/graph_sym_perf/transforms/rule_models.py`：规范 RuleSpec、family/tier 与 factory 类型。
- `src/graph_sym_perf/transforms/registry.py`：唯一 RuleRegistry、历史别名和 active-rule gate。
- `src/graph_sym_perf/transforms/safety_points.py`：统一 SafetyCertificate。
- `src/graph_sym_perf/transforms/symmetry/family.py`：移除跨 MATCH 的 CORE fallback。
- `src/graph_sym_perf/experiment/{plan_models.py,spec.py,planner.py,state.py,lifecycle.py,supervisor.py,worker.py}`：schema-v3、RulePlan、终态和 phase worker 编排。
- `src/graph_sym_perf/core/models.py`、`src/graph_sym_perf/gdsmith/runner.py`：`max_node_num` 到 `--max-node-num`。
- `src/graph_sym_perf/runtime/{models.py,statistics.py,measurement.py,noise.py,oracle.py}`：paired-delta CI、噪声表型和双路径判定。
- `src/graph_sym_perf/validation/{models.py,validator.py,promotion.py}`：pooled validation、submission path 和 reduced-validation gate。
- `src/graph_sym_perf/reducer/{models.py,reducer.py}`：归约后独立复验契约。
- `src/graph_sym_perf/issues/{registry.py,exporter.py}`：family ID、known regression 排除和只导出 SubmitReadyIssue。
- `src/graph_sym_perf/cli/runtime_first.py`：campaign plan/run/status 输出，不新增第二套 CLI。
- `config/systems.yaml`：Neo4j 静态 rule capability 声明。

## Execution Preconditions

实施者开始 Task 1 前执行并保存输出：

```bash
git status --short
git diff --check
.venv/bin/python -m pytest tests/unit tests/property tests/contracts -q
```

预期：工作树仍显示用户已有修改；`git diff --check` 无输出；pytest 只有 Global Constraints 中登记的两个既有失败。若文件集合或失败集合发生变化，停止并重新核对基线，不能把未知差异带入 Task 1。

---

### Task 1: 建立规范 RuleRegistry 与不可覆盖的安全事实

**Files:**
- Modify: `src/graph_sym_perf/transforms/rule_models.py:1-40`
- Modify: `src/graph_sym_perf/transforms/registry.py:1-52`
- Modify: `src/graph_sym_perf/transforms/safety_points.py:1-25`
- Modify: `src/graph_sym_perf/transforms/boolean_equivalence/rules.py:1-4`
- Modify: `tests/unit/transforms/test_rule_registry.py:1-7`
- Modify: `tests/property/test_boolean_rule_soundness.py:1-5`

**Interfaces:**
- Produces: `RuleSpec`, `RuleFamily`, `SafetyTier`, `RuleRegistry.default()`, `RuleRegistry.resolve_id()`, `RuleRegistry.active_rules()`, `SafetyCertificate`。
- Consumes: 现有 `generate_bool_redundancy_family()` 与 `generate_symmetry_family()` factory。

- [ ] **Step 1: 写规范 ID、别名去重和 capability gate 的失败测试**

```python
from graph_sym_perf.transforms.registry import RuleRegistry
from graph_sym_perf.transforms.rule_models import RuleFamily, SafetyTier


def test_default_registry_exposes_the_approved_neo4j_rule_set() -> None:
    registry = RuleRegistry.default()
    core = registry.active_rules(
        dialect="cypher",
        tier=SafetyTier.CORE,
        capabilities=frozenset({"multi_label_intersection"}),
    )
    assert {rule.canonical_rule_id for rule in core} == {
        "constant_true_disjunction",
        "constant_false_conjunction",
        "null_partition_tautology",
        "null_contradiction",
        "null_complete_excluded_middle",
        "pattern_order_symmetry",
        "predicate_order_symmetry",
        "node_order_symmetry",
        "label_order_symmetry",
    }
    assert {rule.family for rule in core} == {RuleFamily.BOOL, RuleFamily.SYM}
    assert all(rule.submit_eligible for rule in core)


def test_legacy_alias_resolves_without_creating_a_second_budget_identity() -> None:
    registry = RuleRegistry.default()
    assert registry.resolve_id("and_excluded_middle_or_null") == "null_complete_excluded_middle"
    assert registry.get("and_excluded_middle_or_null") is registry.get("null_complete_excluded_middle")


def test_label_order_is_fail_closed_without_declared_capability() -> None:
    active = RuleRegistry.default().active_rules(
        dialect="cypher", tier=SafetyTier.CORE, capabilities=frozenset()
    )
    assert "label_order_symmetry" not in {rule.canonical_rule_id for rule in active}
```

- [ ] **Step 2: 运行测试并确认旧 Registry 缺少规范字段和规则**

Run: `.venv/bin/python -m pytest tests/unit/transforms/test_rule_registry.py -q`

Expected: FAIL，至少包含 `cannot import name 'RuleFamily'` 或 `RuleRegistry` 不具备 `resolve_id/active_rules`。

- [ ] **Step 3: 定义 RuleSpec 与统一 SafetyCertificate**

```python
class RuleFamily(str, Enum):
    BOOL = "bool"
    SYM = "sym"


TransformFactory = Callable[[QueryCase, TransformConfig, TargetSystem | None], QueryFamily]


@dataclass(frozen=True)
class RuleSpec:
    canonical_rule_id: str
    family: RuleFamily
    tier: SafetyTier
    submit_eligible: bool
    proof_id: str
    proof_version: str
    purity_requirements: tuple[str, ...]
    supported_dialects: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    allowed_site_classes: tuple[str, ...]
    implementation_rule_id: str
    transform_factory: TransformFactory = field(repr=False, compare=False)


@dataclass(frozen=True)
class SafetyCertificate:
    canonical_rule_id: str
    family: str
    tier: str
    submit_eligible: bool
    proof_id: str
    proof_version: str
    site_class: str
    ast_path: str
    capability_hash: str
    semantic_proof_hash: str
```

删除 `validation.models.SafetyCertificate` 的重复定义留到 Task 8；本任务先让 transform 层成为权威定义。

- [ ] **Step 4: 构建默认 Registry 与历史别名表**

```python
_ALIASES = {"and_excluded_middle_or_null": "null_complete_excluded_middle"}

_BOOL = {
    "constant_true_disjunction": "and_true_or",
    "constant_false_conjunction": "or_false_and",
    "null_partition_tautology": "and_expr_null_partition",
    "null_contradiction": "or_expr_null_contradiction",
    "null_complete_excluded_middle": "and_excluded_middle_or_null",
}

_SYM_CORE = (
    "pattern_order_symmetry",
    "predicate_order_symmetry",
    "node_order_symmetry",
    "label_order_symmetry",
)

_SYM_CONTROL = ("alpha_renaming_symmetry", "edge_notation_symmetry")
```

`RuleRegistry.default()` 为每条 BOOL 绑定 `generate_bool_redundancy_family`，为每条 SYM 绑定 `generate_symmetry_family`；`label_order_symmetry.required_capabilities=("multi_label_intersection",)`，其他首轮规则为空。构造器必须拒绝重复 canonical ID、alias 指向缺失规则、CORE 但 `submit_eligible=false`、CONTROL 但 `submit_eligible=true`。

- [ ] **Step 5: 扩展 BOOL 三值逻辑属性测试**

```python
@pytest.mark.parametrize("value", (True, False, None))
def test_all_core_boolean_identities_preserve_three_valued_truth(value: bool | None) -> None:
    assert constant_true_disjunction(value) is True
    assert constant_false_conjunction(value) is False
    assert null_partition_tautology(value) is True
    assert null_contradiction(value) is False
    assert null_complete_excluded_middle(value) is True
```

在 `boolean_equivalence/rules.py` 增加对应纯 Python truth-table 函数；这些函数只证明恒真/恒假子表达式，不参与 runtime 筛选。

- [ ] **Step 6: 运行 Registry 与 BOOL 属性测试**

Run: `.venv/bin/python -m pytest tests/unit/transforms/test_rule_registry.py tests/property/test_boolean_rule_soundness.py -q`

Expected: PASS，所有规范 ID 唯一，历史别名只解析为同一对象。

- [ ] **Step 7: 提交 Task 1**

```bash
git add src/graph_sym_perf/transforms/rule_models.py src/graph_sym_perf/transforms/registry.py src/graph_sym_perf/transforms/safety_points.py src/graph_sym_perf/transforms/boolean_equivalence/rules.py tests/unit/transforms/test_rule_registry.py tests/property/test_boolean_rule_soundness.py
git commit -m "feat: define canonical rule registry"
```

### Task 2: 将 `pattern_order_symmetry` 收紧为可证明的同 MATCH 结构规则

**Files:**
- Modify: `src/graph_sym_perf/transforms/symmetry/family.py:328-358,815-829`
- Modify: `src/graph_sym_perf/cypher/proof.py:154-225`
- Modify: `tests/unit/test_symmetry_transforms.py:104-133`
- Modify: `tests/property/test_symmetry_rule_soundness.py:1-5`

**Interfaces:**
- Consumes: `parse_match_clause()`、`PatternList(separator="comma")`、`prove_pattern_list_order()`。
- Produces: CORE `pattern_order_symmetry` 只返回同一 MATCH 内的 proved permutation；跨 MATCH 只可由已有 `exploratory_pattern_order_symmetry` 表达，且本 campaign 不调度。

- [ ] **Step 1: 将旧跨 MATCH 正例改成拒绝测试，并增加同 MATCH 正例**

```python
def test_pattern_order_symmetry_reorders_one_comma_pattern_list() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0), (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )
    assert len(family.variants) == 1
    assert family.variants[0].query.startswith("MATCH (b:L1), (a:L0)")
    assert family.variants[0].metadata["semantic_proof"]["theorem"] == "MatchPatternConjunctionCommutativity"


def test_core_pattern_order_never_swaps_match_clauses() -> None:
    family = generate_symmetry_family(
        _case("MATCH (a:L0) MATCH (b:L1) RETURN a.id, b.id"),
        TransformConfig(enabled_rules=("pattern_order_symmetry",)),
    )
    assert not family.variants
    assert family.skipped_variants[0].skip_reason == "NO_REORDERABLE_PATTERN_LIST"
```

- [ ] **Step 2: 运行两个测试并确认跨 MATCH fallback 仍使拒绝测试失败**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetry_transforms.py -k pattern_order -q`

Expected: FAIL 于 `test_core_pattern_order_never_swaps_match_clauses`，因为当前实现仍回退到相邻 MATCH 交换。

- [ ] **Step 3: 删除 CORE 跨 MATCH fallback**

```python
def _pattern_order_symmetry(query: str) -> SymmetryVariant:
    _require_provable_query_scope(query)
    for clause in parse_query(query).ordinary_match_clauses():
        parsed = parse_match_clause(clause)
        pattern = parsed.pattern
        if not isinstance(pattern, AstPatternList) or pattern.separator != "comma" or len(pattern.atoms) < 2:
            continue
        replacement = replace(pattern, atoms=tuple(reversed(pattern.atoms)), text="")
        proof = prove_pattern_list_order_ra(pattern, replacement)
        return SymmetryVariant(
            variant_id="",
            query=query[: parsed.clause.start] + parsed.replace_pattern(replacement.render()) + query[parsed.clause.end :],
            transform_rule="pattern_order_symmetry",
            proof_obligation=proof.obligation,
            metadata={
                **_rule_space_metadata("pattern_order_symmetry"),
                "semantic_proof": proof.to_metadata(),
                "pattern_count": len(pattern.atoms),
                "site_class": "ordinary_match_comma_pattern_list",
            },
        )
    raise TransformSkipError(
        "NO_REORDERABLE_PATTERN_LIST",
        "CORE pattern order requires one ordinary MATCH with a proved comma-separated pattern list",
    )
```

删除 `_prove_pattern_order()` 及 `prove_pattern_order` import；保留明确命名为 exploratory 的跨 MATCH 规则。

- [ ] **Step 4: 增加边界属性测试**

```python
@pytest.mark.parametrize(
    "query",
    (
        "OPTIONAL MATCH (a:L0), (b:L1) RETURN a, b",
        "MATCH (a:L0) WITH a MATCH (b:L1) RETURN a, b",
        "MATCH (a:L0) MATCH (b:L1) RETURN a, b",
    ),
)
def test_core_pattern_order_does_not_cross_scope_boundaries(query: str) -> None:
    family = generate_symmetry_family(
        _case(query), TransformConfig(enabled_rules=("pattern_order_symmetry",))
    )
    assert not family.variants
```

- [ ] **Step 5: 运行 SYM 单元与属性测试**

Run: `.venv/bin/python -m pytest tests/unit/test_symmetry_transforms.py tests/property/test_symmetry_rule_soundness.py -q`

Expected: PASS；跨 MATCH、WITH、OPTIONAL MATCH 均无 CORE variant。

- [ ] **Step 6: 提交 Task 2**

```bash
git add src/graph_sym_perf/transforms/symmetry/family.py src/graph_sym_perf/cypher/proof.py tests/unit/test_symmetry_transforms.py tests/property/test_symmetry_rule_soundness.py
git commit -m "fix: constrain core pattern order symmetry"
```

### Task 3: 增加 schema-v3 campaign 配置与不可变 RulePlan

**Files:**
- Modify: `src/graph_sym_perf/experiment/plan_models.py:12-93`
- Modify: `src/graph_sym_perf/experiment/spec.py:21-200`
- Modify: `src/graph_sym_perf/experiment/planner.py:15-56`
- Modify: `config/systems.yaml:1-9`
- Create: `config/neo4j_validated_runtime_yield.yaml`
- Modify: `tests/unit/test_experiment_spec.py:1-80`
- Modify: `tests/unit/test_experiment_planner.py:1-40`

**Interfaces:**
- Consumes: `RuleRegistry.active_rules()` 和 `config/systems.yaml` 的静态 capability。
- Produces: `CampaignPhase`, `RuleStream`, `RulePlanEntry`, `RulePlan`, `build_rule_plan(spec, phase, selected_cells=())`。

- [ ] **Step 1: 写 schema-v3 精确预算测试**

```python
def test_validated_yield_config_freezes_the_approved_budget() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    assert spec.schema_version == 3
    assert spec.matrix.graph_scales == (128, 256, 512)
    assert spec.campaign.p0.accepted_pairs == 50
    assert spec.campaign.p1.accepted_pairs == 200
    assert spec.campaign.p2.accepted_pairs == 1000
    assert spec.campaign.p2.shards == 3
    assert spec.campaign.p2.max_rule_scale_cells == 6
    assert spec.rules.discovery_rule_ids[0] == "constant_true_disjunction"
    assert "and_excluded_middle_or_null" not in spec.rules.discovery_rule_ids
```

- [ ] **Step 2: 写 RulePlan 展开、seed 隔离和配置不可覆盖测试**

```python
def test_p1_rule_plan_has_independent_rule_scale_budgets() -> None:
    spec = load_experiment_spec("config/neo4j_validated_runtime_yield.yaml")
    plan = build_rule_plan(spec, CampaignPhase.P1)
    discovery = [entry for entry in plan.entries if entry.stream is RuleStream.DISCOVERY]
    control = [entry for entry in plan.entries if entry.stream is RuleStream.CONTROL]
    assert len(discovery) == 9 * 3
    assert len(control) == 2 * 3
    assert all(entry.accepted_pair_quota == 200 for entry in discovery)
    assert all(entry.accepted_pair_quota == 50 for entry in control)
    assert len({entry.shard_seed for entry in plan.entries}) == len(plan.entries)
    assert plan.discovery_accepted_pair_budget == 5400


def test_config_cannot_override_registry_safety_facts(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/neo4j_validated_runtime_yield.yaml").read_text())
    raw["rules"]["tier"] = "experimental"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown field.*rules"):
        load_experiment_spec(path)
```

- [ ] **Step 3: 运行测试并确认 schema-v3 类型尚不存在**

Run: `.venv/bin/python -m pytest tests/unit/test_experiment_spec.py tests/unit/test_experiment_planner.py -q`

Expected: FAIL，包含缺少 `CampaignPhase`、`campaign` 或 `graph_scales`。

- [ ] **Step 4: 定义 plan dataclass 与严格 config dataclass**

```python
class CampaignPhase(str, Enum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"


class RuleStream(str, Enum):
    DISCOVERY = "discovery"
    CONTROL = "control"
    REGRESSION = "regression"


@dataclass(frozen=True)
class RulePlanEntry:
    entry_id: str
    phase: CampaignPhase
    target: TargetSystem
    rule_id: str
    family: str
    tier: str
    graph_scale: int
    shard_index: int
    shard_seed: int
    accepted_pair_quota: int
    max_generated_seed_budget: int
    max_variants_per_seed: int
    proof_version: str
    query_timeout_seconds: int
    max_entry_runtime_seconds: int
    measurement_protocol_id: str
    stream: RuleStream


@dataclass(frozen=True)
class RulePlan:
    spec_name: str
    schema_version: int
    phase: CampaignPhase
    entries: tuple[RulePlanEntry, ...]
    discovery_accepted_pair_budget: int
    control_accepted_pair_budget: int
    source_revision: SourceRevision


@dataclass(frozen=True)
class PhaseBudgetSpec:
    accepted_pairs: int
    shards: int
    max_entry_runtime_seconds: int
    max_rule_scale_cells: int | None = None


@dataclass(frozen=True)
class CampaignBudgetSpec:
    p0: PhaseBudgetSpec
    p1: PhaseBudgetSpec
    p2: PhaseBudgetSpec
    generated_seed_multiplier: int


@dataclass(frozen=True)
class RuleSelectionSpec:
    discovery_rule_ids: tuple[str, ...]
    control_rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class CampaignMatrixSpec:
    graph_scales: tuple[int, ...]


@dataclass(frozen=True)
class ValidatedYieldExperimentSpec:
    schema_version: int
    name: str
    seed: int
    targets: tuple[TargetSystem, ...]
    matrix: CampaignMatrixSpec
    campaign: CampaignBudgetSpec
    rules: RuleSelectionSpec
    generation: GenerationSpec
    runtime: Mapping[str, object]
    validation: Mapping[str, object]
    reducer: Mapping[str, object]
    regression: Mapping[str, object]
    artifacts: Mapping[str, object]
    systems: Mapping[TargetSystem, SystemDefinition]
    source_path: Path
```

schema-v2 继续由现有 `ExperimentSpec/RunPlan` 加载，用于历史运行；schema-v3 解析为 `ValidatedYieldExperimentSpec`，正式 validated-yield run 不得回落到 `ShardTask`。

- [ ] **Step 5: 写唯一的新 campaign YAML**

```yaml
schema_version: 3
experiment: {name: neo4j_validated_runtime_yield, seed: 20260714}
targets: [neo4j]
matrix: {graph_scales: [128, 256, 512]}
campaign:
  p0: {accepted_pairs: 50, shards: 1, max_entry_runtime_seconds: 3600}
  p1: {accepted_pairs: 200, shards: 1, max_entry_runtime_seconds: 7200}
  p2: {accepted_pairs: 1000, shards: 3, max_entry_runtime_seconds: 21600, max_rule_scale_cells: 6}
  generated_seed_multiplier: 10
rules:
  discovery_rule_ids: [constant_true_disjunction, constant_false_conjunction, null_partition_tautology, null_contradiction, null_complete_excluded_middle, pattern_order_symmetry, predicate_order_symmetry, node_order_symmetry, label_order_symmetry]
  control_rule_ids: [alpha_renaming_symmetry, edge_notation_symmetry]
generation: {backend: gdsmith, source: gdsmith, algorithm: COMPARED3, commit_source: vendored_checkout, checkout: ../../GDsmith-src}
runtime:
  noise: {probe_queries: 20, aa_pairs_per_query: 10, absolute_delta_quantile: 0.99}
  stage1: {warmups: 2, pairs: 3, top_fraction: 0.05, min_top_k: 3, max_top_k: 30}
  stage2: {warmups: 3, pairs: 12, relative_ratio: 1.5, relative_ci_low: 1.1, absolute_delta_ms: 10.0, direction_consistency: 0.75}
validation:
  sessions: 2
  warmups_per_session: 5
  pairs_per_session: 20
  bootstrap_resamples: 10000
  relative: {session_ratio: 2.0, pooled_ratio: 3.0, ratio_ci_low: 2.0}
  absolute: {noise_multiplier: 5.0, delta_ms: 50.0, pooled_ratio: 1.5, ratio_ci_low: 1.2}
  max_result_rows: 100000
reducer: {enabled: true, max_attempts: 500, max_seconds: 1800, require_reduced_validation: true}
regression: {manifest: tests/fixtures/regressions/neo4j_bug001/manifest.yaml}
artifacts: {entry_budget_mb: 500}
```

`config/systems.yaml` 的 Neo4j 条目增加 `rule_capabilities: [multi_label_intersection]`；该字段只能声明 adapter/system capability，不能改变规则证明。

- [ ] **Step 6: 实现确定性 RulePlan 展开**

```python
def _entry_seed(base: int, phase: CampaignPhase, stream: RuleStream, rule_id: str, scale: int, shard: int) -> int:
    payload = f"{base}:{phase.value}:{stream.value}:{rule_id}:{scale}:{shard}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1)
```

P0 展开 active CORE×128×1、CONTROL×128×1 和 regression manifest entries；P1 展开 active CORE×三规模×1 与 CONTROL×三规模×1；P2 必须显式收到 `selected_cells`，为空或超过 6 个时 fail-closed。每个 entry 把 Registry 的 canonical ID、family、tier、proof version 与 system timeout 冻结进 manifest。

- [ ] **Step 7: 运行 schema/plan 测试并检查 resolved manifest**

Run: `.venv/bin/python -m pytest tests/unit/test_experiment_spec.py tests/unit/test_experiment_planner.py -q`

Expected: PASS；P1 discovery budget 恰为 5400，control budget 恰为 300。

Run: `.venv/bin/python -c 'from graph_sym_perf.experiment.plan_models import CampaignPhase; from graph_sym_perf.experiment.planner import build_rule_plan, write_resolved_manifest; from graph_sym_perf.experiment.spec import load_experiment_spec; write_resolved_manifest(build_rule_plan(load_experiment_spec("config/neo4j_validated_runtime_yield.yaml"), CampaignPhase.P1), "/tmp/neo4j-p1-plan.json")'`

Expected: exit 0；JSON 中每个 entry 均有独立 `rule_id`、`graph_scale`、quota、seed、proof version 和 timeout。

- [ ] **Step 8: 提交 Task 3**

```bash
git add src/graph_sym_perf/experiment/plan_models.py src/graph_sym_perf/experiment/spec.py src/graph_sym_perf/experiment/planner.py config/systems.yaml config/neo4j_validated_runtime_yield.yaml tests/unit/test_experiment_spec.py tests/unit/test_experiment_planner.py
git commit -m "feat: plan validated yield rule budgets"
```

### Task 4: 将 GDsmith `--max-node-num` 与实际图 manifest 接入每个 entry

**Files:**
- Modify: `src/graph_sym_perf/core/models.py:65-75`
- Modify: `src/graph_sym_perf/gdsmith/runner.py:94-172`
- Modify: `src/graph_sym_perf/generation/corpus.py:1-19`
- Modify: `src/graph_sym_perf/adapters/neo4j.py:1-120`
- Modify: `src/graph_sym_perf/experiment/worker.py:235-264`
- Modify: `tests/unit/test_gdsmith.py:1-230`
- Modify: `tests/unit/test_neo4j_adapter.py`
- Modify: `tests/unit/test_runtime_first_worker.py:1-170`

**Interfaces:**
- Consumes: `RulePlanEntry.graph_scale`。
- Produces: `GdsmithConfig.max_node_num`、命令行 `--max-node-num`、`GraphManifest` 与图内容指纹。

- [ ] **Step 1: 写 GDsmith 命令与 worker 透传失败测试**

```python
def test_gdsmith_command_exposes_max_node_num() -> None:
    command = build_gdsmith_command(
        java_path="java",
        jar_path="GDsmith.jar",
        config=GdsmithConfig(max_node_num=512),
        backend="neo4j",
        host="127.0.0.1",
        port=7687,
        username="neo4j",
        password="secret",
        num_queries=10,
        seed=7,
    )
    index = command.index("--max-node-num")
    assert command[index + 1] == "512"
```

```python
def test_worker_passes_rule_plan_graph_scale_to_gdsmith(monkeypatch, tmp_path) -> None:
    captured = {}
    def fake_run_gdsmith(**kwargs):
        captured["config"] = kwargs["config"]
        return _empty_gdsmith_result()
    monkeypatch.setattr("graph_sym_perf.experiment.worker.run_gdsmith", fake_run_gdsmith)
    worker = _campaign_worker(tmp_path)
    worker._generate_with_gdsmith(_entry(graph_scale=256), tmp_path / "raw")
    assert captured["config"].max_node_num == 256
```

- [ ] **Step 2: 运行测试并确认参数不存在**

Run: `.venv/bin/python -m pytest tests/unit/test_gdsmith.py tests/unit/test_runtime_first_worker.py -k 'max_node or graph_scale' -q`

Expected: FAIL，`GdsmithConfig` 不接受 `max_node_num`。

- [ ] **Step 3: 增加配置字段和命令参数**

```python
@dataclass(frozen=True)
class GdsmithConfig:
    # existing fields stay unchanged
    max_node_num: int = 128
```

在 `build_gdsmith_command()` 中、backend token 之前加入：

```python
"--max-node-num",
str(config.max_node_num),
```

构造 `GdsmithConfig` 时拒绝非正整数；worker 使用 `replace(base_config, max_node_num=entry.graph_scale)`，不能把 scale 塞入不透明 `extra_args`。

- [ ] **Step 4: 写 Neo4j 实际图统计测试**

```python
def test_graph_manifest_records_requested_and_observed_scale() -> None:
    adapter = _ResultSequenceAdapter(
        rows=[
            ({"nodes": 256},),
            ({"relationships": 430},),
            ({"labels": 5},),
            ({"types": 4},),
            ({"indexes": 3},),
        ]
    )
    manifest = read_neo4j_graph_manifest(adapter, requested_max_nodes=256, statements=("CREATE (n {id: 1})",))
    assert manifest.requested_max_nodes == 256
    assert manifest.actual_nodes == 256
    assert manifest.actual_relationships == 430
    assert manifest.graph_fingerprint
```

- [ ] **Step 5: 实现 GraphManifest 与普通 execute 统计**

```python
@dataclass(frozen=True)
class GraphManifest:
    requested_max_nodes: int
    actual_nodes: int
    actual_relationships: int
    label_count: int
    relationship_type_count: int
    index_count: int
    graph_fingerprint: str
```

`read_neo4j_graph_manifest()` 只执行普通 metadata/query calls，依次取得节点、关系、label、relationship type 和 index 数量；`graph_fingerprint` 为规范化 graph setup statements 的 SHA-256。任何字段缺失或非负整数检查失败都使 entry fail-closed，不允许用请求规模代替实际规模。

- [ ] **Step 6: 将 manifest 写入 entry attempt 目录**

worker 在 `_reset_and_load()` 后立即写 `graph_manifest.json`，并把 requested/actual scale、graph fingerprint 加入 entry summary。validation 使用该 fingerprint，不重新散列路径或文件名。

- [ ] **Step 7: 运行 GDsmith、Neo4j adapter 和 worker 测试**

Run: `.venv/bin/python -m pytest tests/unit/test_gdsmith.py tests/unit/test_neo4j_adapter.py tests/unit/test_runtime_first_worker.py -q`

Expected: PASS；命令和 artifact 中均显示正确 scale，密码仍被 redaction。

- [ ] **Step 8: 提交 Task 4**

```bash
git add src/graph_sym_perf/core/models.py src/graph_sym_perf/gdsmith/runner.py src/graph_sym_perf/generation/corpus.py src/graph_sym_perf/adapters/neo4j.py src/graph_sym_perf/experiment/worker.py tests/unit/test_gdsmith.py tests/unit/test_neo4j_adapter.py tests/unit/test_runtime_first_worker.py
git commit -m "feat: scale gdsmith graphs per rule entry"
```

### Task 5: 用 RulePlanEntry 驱动 certified pair 构建与独立预算

**Files:**
- Create: `src/graph_sym_perf/workload/rule_pairs.py`
- Modify: `src/graph_sym_perf/workload/models.py:1-20`
- Modify: `src/graph_sym_perf/workload/acceptance.py:1-23`
- Modify: `src/graph_sym_perf/transforms/registry.py`
- Modify: `src/graph_sym_perf/experiment/state.py:8-35`
- Modify: `src/graph_sym_perf/experiment/worker.py:66-220,267-299`
- Modify: `tests/unit/workload/test_acceptance.py`
- Modify: `tests/unit/test_runtime_first_worker.py`
- Modify: `tests/integration/test_end_to_end_acceptance.py`

**Interfaces:**
- Consumes: `RulePlanEntry`、`RuleRegistry`、单条 rule 的 transform factory、完整 typed equivalence checker。
- Produces: `CertifiedQueryPair`、`PairBuildOutcome`、每 entry 独立 top-up 与明确终态。

- [ ] **Step 1: 写同一 query 分别执行 BOOL/SYM entry 的失败测试**

```python
def test_rule_pair_factory_uses_the_entry_rule_not_a_worker_constant() -> None:
    factory = RulePairFactory(RuleRegistry.default())
    query = "MATCH (a:L0), (b:L1) WHERE a.id = 1 RETURN a.id, b.id"
    bool_result = factory.build(query, _entry(rule_id="null_partition_tautology", family="bool"), TargetSystem.NEO4J)
    sym_result = factory.build(query, _entry(rule_id="pattern_order_symmetry", family="sym"), TargetSystem.NEO4J)
    assert bool_result.pair is not None
    assert sym_result.pair is not None
    assert bool_result.pair.certificate.canonical_rule_id == "null_partition_tautology"
    assert sym_result.pair.certificate.canonical_rule_id == "pattern_order_symmetry"
    assert bool_result.pair.query_b != sym_result.pair.query_b
```

- [ ] **Step 2: 写 worker 独立 quota 与 underfilled 测试**

```python
def test_worker_never_transfers_an_underfilled_rule_budget(tmp_path) -> None:
    worker = _worker_with_queries(tmp_path, queries=("MATCH (n) RETURN n",))
    result = worker(_entry(rule_id="pattern_order_symmetry", accepted_pair_quota=2, max_generated_seed_budget=1))
    assert result["accepted_pairs"] == 0
    assert result["terminal_state"] == "underfilled"
    assert result["rule_id"] == "pattern_order_symmetry"


def test_worker_stops_at_the_entry_wall_clock_deadline(tmp_path) -> None:
    worker = _worker_with_monotonic_clock(tmp_path, values=(0.0, 3601.0))
    result = worker(_entry(max_entry_runtime_seconds=3600))
    assert result["terminal_state"] == "resource_exhausted"
```

- [ ] **Step 3: 运行测试并确认 worker 仍硬编码 BOOL helper**

Run: `.venv/bin/python -m pytest tests/unit/workload/test_acceptance.py tests/unit/test_runtime_first_worker.py -k 'entry_rule or underfilled' -q`

Expected: FAIL；当前 worker 调用 `_certified_boolean_pair()` 且 summary 不含 entry terminal state。

- [ ] **Step 4: 定义 certified pair 结果类型**

```python
@dataclass(frozen=True)
class CertifiedQueryPair:
    pair_hash: str
    query_a: str
    query_b: str
    parameters: Mapping[str, object]
    certificate: SafetyCertificate
    transform_metadata: Mapping[str, object]


@dataclass(frozen=True)
class PairBuildOutcome:
    pair: CertifiedQueryPair | None
    reject_code: str | None = None
    quarantine: bool = False
```

- [ ] **Step 5: 实现 RulePairFactory 的单规则执行**

`RulePairFactory.build()` 必须：

1. 由 `registry.get(entry.rule_id)` 重新核验 canonical ID、family、tier、proof version；
2. 用 `TransformConfig(enabled_families=(...), enabled_rules=(spec.implementation_rule_id,), max_variants_per_case=1, gdsmith_safe_only=True)` 调用该 RuleSpec 的 factory；
3. 将普通“不适用/证明前置条件拒绝”映射到 `RULE_NOT_APPLICABLE` 或 `PROOF_PRECONDITION_REJECTED`；
4. 要求恰好一个 static-safe variant，且 metadata 内存在 `semantic_proof`；
5. 对 proof metadata 规范 JSON 做 SHA-256，签发统一 `SafetyCertificate`；
6. 任何 rule ID、proof version、site class 或 semantic proof 内部不变量不一致返回 `quarantine=True`。

- [ ] **Step 6: 将 worker 改为只消费 RulePlanEntry**

删除 `_certified_boolean_pair()`。worker 的 acceptance loop 调用 `RulePairFactory`，随后执行 dialect/replay gate、两侧普通 execute、truncation gate 和完整 typed equivalence。CORE mismatch 立即终止 entry 为 `QUARANTINED` 并保存 query/proof 反例；普通 query execution error 只拒绝当前 pair。达到 `max_generated_seed_budget` 仍不足 quota 时终态为 `UNDERFILLED`。每次生成或测量前用 `time.monotonic()` 检查 entry deadline，超过即 `RESOURCE_EXHAUSTED`；DBMS restart、OOM、连接失效或 graph fingerprint 漂移一律为 `INFRASTRUCTURE_FAILED`。

`EntryState` 固定为：

```python
class EntryState(str, Enum):
    PLANNED = "planned"
    GENERATING = "generating"
    BUILDING_ACCEPTED_PAIRS = "building_accepted_pairs"
    STAGE1_COMPLETE = "stage1_complete"
    STAGE2_COMPLETE = "stage2_complete"
    VALIDATION_COMPLETE = "validation_complete"
    REDUCTION_COMPLETE = "reduction_complete"
    MATERIALIZED = "materialized"
    COMPLETED = "completed"
    UNDERFILLED = "underfilled"
    QUARANTINED = "quarantined"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
```

- [ ] **Step 7: 运行 acceptance/worker/integration 测试**

Run: `.venv/bin/python -m pytest tests/unit/workload/test_acceptance.py tests/unit/test_runtime_first_worker.py tests/integration/test_end_to_end_acceptance.py -q`

Expected: PASS；测试 fixture 中至少一个 BOOL 和一个 SYM entry 各自达到 quota，summary 的 rule/stream 统计不混合。

- [ ] **Step 8: 提交 Task 5**

```bash
git add src/graph_sym_perf/workload/rule_pairs.py src/graph_sym_perf/workload/models.py src/graph_sym_perf/workload/acceptance.py src/graph_sym_perf/transforms/registry.py src/graph_sym_perf/experiment/state.py src/graph_sym_perf/experiment/worker.py tests/unit/workload/test_acceptance.py tests/unit/test_runtime_first_worker.py tests/integration/test_end_to_end_acceptance.py
git commit -m "feat: execute rule plan entries in worker"
```

### Task 6: 保存每个 accepted pair 的紧凑 runtime record 并标定 shard 噪声

**Files:**
- Create: `src/graph_sym_perf/runtime/records.py`
- Modify: `src/graph_sym_perf/runtime/models.py:9-56`
- Modify: `src/graph_sym_perf/runtime/statistics.py:10-60`
- Modify: `src/graph_sym_perf/runtime/measurement.py:29-132`
- Modify: `src/graph_sym_perf/runtime/noise.py:10-30`
- Modify: `src/graph_sym_perf/experiment/worker.py`
- Create: `tests/unit/runtime/test_records.py`
- Modify: `tests/unit/runtime/test_statistics.py`
- Modify: `tests/unit/runtime/test_measurement.py`
- Modify: `tests/unit/runtime/test_oracle.py`
- Modify: `tests/unit/test_runtime_first_worker.py`

**Interfaces:**
- Consumes: `CertifiedQueryPair`、Stage-1 `RuntimeEvidence`、GraphManifest 和 NoiseCalibration。
- Produces: `bootstrap_delta_ci()`、扩展 `RuntimeEvidence`、`PairRuntimeRecord`、`CompactRuntimeStore`、`pair_runtime.jsonl`。

- [ ] **Step 1: 写 paired-delta CI 测试**

```python
def test_bootstrap_delta_ci_preserves_paired_direction() -> None:
    samples = ((10.0, 25.0), (11.0, 27.0), (9.0, 24.0), (10.0, 26.0))
    interval = bootstrap_delta_ci(samples, resamples=1000, seed=7)
    assert interval.low > 0
    assert interval.high >= interval.low
```

- [ ] **Step 2: 写 compact record 精度和 fail-closed 测试**

```python
def test_pair_runtime_record_stores_integer_microseconds_without_query_text(tmp_path) -> None:
    record = PairRuntimeRecord.from_evidence(
        pair=_certified_pair(),
        entry=_entry(),
        evidence=_evidence(a=(1.234, 1.235, 1.236), b=(5.678, 5.679, 5.680)),
        noise_floor_id="noise-1",
        noise_p99_ms=0.2,
    )
    store = CompactRuntimeStore(tmp_path / "pair_runtime.jsonl")
    store.append(record)
    payload = json.loads((tmp_path / "pair_runtime.jsonl").read_text())
    assert payload["samples_us"][0][1:] == [1234, 5678]
    assert "query_a" not in payload and "query_b" not in payload


def test_store_write_failure_does_not_increment_accepted_count(monkeypatch, tmp_path) -> None:
    store = CompactRuntimeStore(tmp_path / "pair_runtime.jsonl")
    monkeypatch.setattr(store, "_write_line", lambda _line: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ArtifactWriteError, match="disk full"):
        store.append(_runtime_record())
    assert store.count == 0
```

- [ ] **Step 3: 运行测试并确认 delta CI 与 record store 尚不存在**

Run: `.venv/bin/python -m pytest tests/unit/runtime/test_statistics.py tests/unit/runtime/test_records.py -q`

Expected: FAIL，缺少 `bootstrap_delta_ci` 和 `runtime.records`。

- [ ] **Step 4: 扩展 RuntimeEvidence 与统计函数**

```python
@dataclass(frozen=True)
class RuntimeEvidence:
    # existing fields remain
    median_a_ms: float | None = None
    median_b_ms: float | None = None
    slower_side: str | None = None
    ci_delta_low_ms: float | None = None
    ci_delta_high_ms: float | None = None
```

measurement 先由完整样本的两侧 median 确定 `slower_side`，然后统一计算 `median_ratio=slow/fast`、`median_delta_ms=slow-fast` 和与该方向一致的 sample fraction；A 慢与 B 慢必须对称支持。`bootstrap_delta_ci()` 对 paired `(a,b)` 样本有放回重采样，统计量为按冻结 `slower_side` 定向后的 `median(slow-fast)`；ratio CI 同样使用 slow/fast。零差不计为任一方向，后续两个 validation session 必须得到相同 `slower_side`。

- [ ] **Step 5: 扩展 NoiseCalibration 表型**

```python
@dataclass(frozen=True)
class NoiseCalibration:
    valid: bool
    sample_count: int
    p95_delta_ms: float | None
    p99_delta_ms: float | None
    median_order_bias_ms: float | None
    long_tail_ratio: float | None
    reason_codes: tuple[str, ...]
```

worker 从已通过完整 equivalence 的 pairs 按 acceptance base runtime 排序，选择 20 个等距分位点作为 probe；每条 10 个 A/A pair。定义 `median_order_bias_ms = abs(median(signed_delta_AB) - median(signed_delta_BA))`，`long_tail_ratio = p99 / max(p95, 0.001)`；少于 200 个完整样本、`median_order_bias_ms > p95_delta_ms` 或 `long_tail_ratio > 5.0` 时 calibration invalid，entry 不进入 discovery。这些数值写入 immutable runtime protocol，不能观察数据后调整。

- [ ] **Step 6: 定义紧凑 record 与 durable append**

```python
@dataclass(frozen=True)
class PairRuntimeRecord:
    pair_hash: str
    rule_id: str
    family: str
    stream: str
    phase: str
    graph_scale: int
    shard_seed: int
    query_a_hash: str
    query_b_hash: str
    site_class: str
    proof_version: str
    safety_certificate_hash: str
    equivalence_summary: str
    noise_floor_id: str
    samples_us: tuple[tuple[str, int | None, int | None], ...]
    median_a_ms: float | None
    median_b_ms: float | None
    median_delta_ms: float | None
    median_ratio: float | None
    runtime_direction: str | None
    delta_over_noise: float | None
    direction_consistency: float
    timeout_state: str
    screen_stage: str
    protocol_version: str
    lifecycle_status: str
```

`CompactRuntimeStore.append()` 使用单 writer、规范 JSON、flush 和 `os.fsync()`；只有 durable append 成功后 worker 才递增 accepted count。`pair_runtime.jsonl` 是 accepted+Stage-1 的唯一权威逐 pair 文件，不再另写会产生双写不一致的 `accepted_pairs.jsonl`。

- [ ] **Step 7: 将 worker 改成“先等价、再噪声、再逐 pair Stage-1 durable append”**

worker 在内存中保留当前 entry 的 certified pair 和 acceptance runtime，完成 noise calibration 后逐 pair 测量 Stage-1 并写 record。Stage-1 即使 timeout 也写 timeout record；artifact 写入异常将 entry 终止为 `INFRASTRUCTURE_FAILED`，不得生成 partial-complete summary。

- [ ] **Step 8: 运行 runtime 与 worker 测试**

Run: `.venv/bin/python -m pytest tests/unit/runtime tests/unit/test_runtime_first_worker.py -q`

Expected: PASS；`accepted_pairs == CompactRuntimeStore.count`，record 不含完整 query、PROFILE 或 plan。

- [ ] **Step 9: 提交 Task 6**

```bash
git add src/graph_sym_perf/runtime/records.py src/graph_sym_perf/runtime/models.py src/graph_sym_perf/runtime/statistics.py src/graph_sym_perf/runtime/measurement.py src/graph_sym_perf/runtime/noise.py src/graph_sym_perf/experiment/worker.py tests/unit/runtime/test_records.py tests/unit/runtime/test_statistics.py tests/unit/runtime/test_measurement.py tests/unit/runtime/test_oracle.py tests/unit/test_runtime_first_worker.py
git commit -m "feat: persist compact paired runtime evidence"
```

### Task 7: 实现 Stage-1 三队列宽召回和 Stage-2 双宽门槛

**Files:**
- Create: `src/graph_sym_perf/runtime/screening.py`
- Modify: `src/graph_sym_perf/runtime/oracle.py:8-28`
- Modify: `src/graph_sym_perf/experiment/worker.py`
- Create: `tests/unit/runtime/test_screening.py`
- Modify: `tests/unit/runtime/test_oracle.py`
- Modify: `tests/unit/test_runtime_first_worker.py`

**Interfaces:**
- Consumes: `PairRuntimeRecord`、`NoiseCalibration`、Stage-2 `RuntimeEvidence`。
- Produces: `stage1_top_k()`、`evaluate_stage2()`、`Stage2Decision`、candidate evidence directory 和 timeout queue。

- [ ] **Step 1: 写 K1 和三队列去重测试**

```python
def test_stage1_top_k_uses_abs_ratio_and_noise_queues() -> None:
    records = _rank_fixture_records(accepted=200)
    selected = stage1_top_k(records, noise_p99_ms=2.0)
    assert len(selected) == 10
    assert _pair("largest_abs") in selected
    assert _pair("largest_ratio_above_half_noise") in selected
    assert _pair("largest_noise_normalized") in selected
    assert len(set(selected)) == len(selected)


@pytest.mark.parametrize("accepted, expected", ((50, 3), (200, 10), (1000, 30)))
def test_k1_formula(accepted: int, expected: int) -> None:
    assert stage1_k(accepted) == expected
```

- [ ] **Step 2: 写 Stage-2 relative/absolute 边界测试**

```python
def test_stage2_relative_wide_gate() -> None:
    decision = evaluate_stage2(_evidence(ratio=1.6, ratio_ci_low=1.2, delta=8.0, delta_ci_low=1.0, direction=0.83), noise_p99_ms=5.0)
    assert decision.relative_pass and not decision.absolute_pass


def test_stage2_absolute_wide_gate() -> None:
    decision = evaluate_stage2(_evidence(ratio=1.2, ratio_ci_low=1.05, delta=25.0, delta_ci_low=4.0, direction=0.92), noise_p99_ms=8.0)
    assert decision.absolute_pass and not decision.relative_pass


def test_stage2_rejects_large_ratio_inside_noise() -> None:
    decision = evaluate_stage2(_evidence(ratio=10.0, ratio_ci_low=2.0, delta=0.1, delta_ci_low=-0.1, direction=0.9), noise_p99_ms=1.0)
    assert not decision.promote
```

- [ ] **Step 3: 运行测试并确认 screening 模块不存在**

Run: `.venv/bin/python -m pytest tests/unit/runtime/test_screening.py -q`

Expected: FAIL，`ModuleNotFoundError: graph_sym_perf.runtime.screening`。

- [ ] **Step 4: 实现确定性 Top-K**

```python
def stage1_k(accepted_pairs: int) -> int:
    return min(30, max(3, math.ceil(0.05 * accepted_pairs)))
```

`stage1_top_k()` 为 `Q_abs`、`Q_ratio`、`Q_noise` 各分配 `ceil(K/3)` 名额；ratio 队列要求 `median_delta_ms >= 0.5 * noise_p99_ms`。各队列以 score 降序、`pair_hash` 升序打破平局，按 abs→ratio→noise round-robin 合并并去重，最后截断到 K。

- [ ] **Step 5: 实现 Stage2Decision**

```python
@dataclass(frozen=True)
class Stage2Decision:
    relative_pass: bool
    absolute_pass: bool
    reason_codes: tuple[str, ...]

    @property
    def promote(self) -> bool:
        return self.relative_pass or self.absolute_pass


@dataclass(frozen=True)
class Stage2Candidate:
    pair_hash: str
    decision: Stage2Decision
    noise_normalized_score: float
    evidence: RuntimeEvidence
```

relative wide gate 必须同时满足 ratio≥1.5、ratio CI low>1.1、delta>noise p99、direction≥0.75；absolute wide gate 必须同时满足 delta≥max(2×noise p99,10ms)、delta CI low>0、ratio>1.1、direction≥0.75。

- [ ] **Step 6: 接入 worker 的 Stage-2 evidence 升级**

worker 只对 `stage1_top_k()` 返回的 pair 执行 3 warmups+12 balanced pairs。Stage-2 candidate 目录保存完整 A/B query、certificate、Stage-1/2 evidence 和 graph manifest；未入选 pair 只保留 compact record。P1 每 rule-scale 最多 2 个进入 validation，P2 每 shard 最多 5 个；一侧 timeout 且 DBMS health 正常写入 `timeout_queue.jsonl`，health event 转 ReliabilityFinding。

- [ ] **Step 7: 运行 screening 和 worker 测试**

Run: `.venv/bin/python -m pytest tests/unit/runtime/test_screening.py tests/unit/runtime/test_oracle.py tests/unit/test_runtime_first_worker.py -q`

Expected: PASS；200-pair cell 最多 10 个 Stage-2，1000-pair cell 最多 30 个，名额不强制用满。

- [ ] **Step 8: 提交 Task 7**

```bash
git add src/graph_sym_perf/runtime/screening.py src/graph_sym_perf/runtime/oracle.py src/graph_sym_perf/experiment/worker.py tests/unit/runtime/test_screening.py tests/unit/runtime/test_oracle.py tests/unit/test_runtime_first_worker.py
git commit -m "feat: add two-stage runtime screening"
```

### Task 8: 实现双进程 independent validation、OR 提交路径与 CONTROL 复验

**Files:**
- Create: `src/graph_sym_perf/validation/session_runner.py`
- Modify: `src/graph_sym_perf/runtime/screening.py`
- Modify: `src/graph_sym_perf/validation/models.py:1-100`
- Modify: `src/graph_sym_perf/validation/validator.py:23-66`
- Modify: `src/graph_sym_perf/validation/promotion.py:1-25`
- Modify: `src/graph_sym_perf/experiment/worker.py`
- Modify: `tests/unit/validation/test_promotion.py`
- Modify: `tests/integration/test_issue_validation.py`
- Modify: `tests/unit/test_runtime_first_worker.py`

**Interfaces:**
- Consumes: Stage-2 candidate、GraphManifest、统一 transform `SafetyCertificate`。
- Produces: `SubmissionPath`、pooled `IssueValidationReport`、`SpawnedValidationSessionRunner` 和 `control_gate_hit/control_validation_pass` 诊断。

- [ ] **Step 1: 写 relative 与 absolute 最终路径边界测试**

```python
def test_relative_submission_path_requires_both_sessions_and_pooled_ci() -> None:
    report = IssueValidator().validate(
        _pair(),
        _runner(
            _session(ratio=3.2, delta=80.0),
            _session(ratio=3.1, delta=75.0),
        ),
        discovery_ratio=3.4,
        noise_floor_ms=5.0,
    )
    assert report.stage is PipelineStage.VALIDATION_PASSED
    assert report.submission_path is SubmissionPath.RELATIVE
    assert report.pooled_ratio_ci_low >= 2.0


def test_absolute_submission_path_accepts_stable_large_delta() -> None:
    report = IssueValidator().validate(
        _pair(),
        _runner(
            _session(ratio=1.8, delta=90.0),
            _session(ratio=1.7, delta=85.0),
        ),
        discovery_ratio=1.9,
        noise_floor_ms=10.0,
    )
    assert report.stage is PipelineStage.VALIDATION_PASSED
    assert report.submission_path is SubmissionPath.ABSOLUTE
    assert report.pooled_delta_ci_low_ms > 10.0
```

- [ ] **Step 2: 写 independence、重载和 CONTROL 固定抽样测试**

```python
def test_validator_rejects_same_process_sessions() -> None:
    report = IssueValidator().validate(_pair(), _runner(_session(process_id=17), _session(process_id=17)), discovery_ratio=4.0)
    assert report.stage is PipelineStage.REJECTED
    assert "sessions_not_independent" in report.reason_codes


def test_control_selection_validates_gate_hits_or_one_top_score() -> None:
    assert select_control_validation_pairs(_stage2_candidates(gate_hits=3)) == _top_gate_hits(2)
    assert select_control_validation_pairs(_stage2_candidates(gate_hits=0)) == (_top_noise_score(),)
```

- [ ] **Step 3: 运行 validation 测试并确认当前 validator 只有单一 AND gate**

Run: `.venv/bin/python -m pytest tests/integration/test_issue_validation.py tests/unit/validation/test_promotion.py -q`

Expected: FAIL，缺少 `SubmissionPath`、pooled delta CI 或 absolute path。

- [ ] **Step 4: 统一 validation models 并实现 pooled gate**

从 `validation.models` 删除重复 `SafetyCertificate`，改为 import `transforms.safety_points.SafetyCertificate`。新增：

```python
class SubmissionPath(str, Enum):
    RELATIVE = "relative"
    ABSOLUTE = "absolute"


@dataclass(frozen=True)
class IssueValidationReport:
    stage: PipelineStage
    equivalence: EquivalenceEvidence
    sessions: tuple[ValidationSession, ...]
    discovery_ratio: float | None
    validated_ratio: float | None
    pooled_delta_ms: float | None
    pooled_ratio_ci_low: float | None
    pooled_delta_ci_low_ms: float | None
    submission_path: SubmissionPath | None
    magnitude_stability: str
    reason_codes: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)
```

validator 先要求两个 session 的 `slower_side` 相同，再按该方向汇总 40 个 paired samples并重新做 10000 次 pooled bootstrap。relative path 要求每 session ratio≥2、pooled ratio≥3、ratio CI low≥2、delta>noise；absolute path 要求两 session 同方向且各自 delta>0、pooled delta≥max(5×noise,50ms)、delta CI low>noise、pooled ratio≥1.5、ratio CI low≥1.2。两条都通过时固定记录 `relative`，同时在 metadata 记录 `absolute_also_passed=true`。两个 session 的 measurement schedule seed 由 candidate hash 和 session number 分别派生，不能复用 discovery seed。

`select_control_validation_pairs(candidates: tuple[Stage2Candidate, ...]) -> tuple[Stage2Candidate, ...]` 放在 `runtime.screening`：按 final-gate hit、noise-normalized score、pair hash 确定性排序；有 hit 时最多取 2 个，无 hit 时取 1 个最高分 pair。

- [ ] **Step 5: 实现真正的独立进程 session runner**

```python
@dataclass(frozen=True)
class ValidationSessionRequest:
    target: str
    graph_statements: tuple[str, ...]
    pair: EquivalentQueryPair
    policy: RuntimePolicy
    expected_graph_fingerprint: str
    expected_dbms_fingerprint: str


class SpawnedValidationSessionRunner:
    def run_session(self, pair: EquivalentQueryPair, session_number: int) -> tuple[EquivalenceEvidence, ValidationSession]:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_validation_session_entry, args=(self._request(pair, session_number), queue))
        process.start()
        process.join(self.hard_timeout_seconds)
        if process.is_alive():
            process.kill()
            process.join()
            raise RuntimeError("VALIDATION_SESSION_TIMEOUT")
        status, payload = queue.get(timeout=1.0)
        if status != "ok":
            raise RuntimeError(f"VALIDATION_SESSION_FAILED: {payload}")
        return payload
```

顶层 `_validation_session_entry()` 在子进程内新建 adapter、清空并重载 graph、重新计算 graph/dbms fingerprint、执行完整 typed equivalence、执行 5 warmups+20 balanced pairs、检查 health event，finally 关闭 adapter。父进程不得复用 discovery adapter/connection。

timeout queue 使用同样的两个独立进程和原冻结 timeout；只有两 session 都在同一侧 timeout、另一侧完成、graph/dbms fingerprint 一致且无 health event 时记录 `timeout_validation_confirmed`。由于没有完整 paired CI，该状态保持诊断性 `INCONCLUSIVE`，不能绕过 relative/absolute CI 门槛生成 SubmitReadyIssue。

- [ ] **Step 6: 将 CONTROL 诊断接入 worker**

discovery stream 只复验 Stage-2 promote candidates；CONTROL stream 对所有 gate hit 最多取 2 个，若无 gate hit 则取综合 noise-normalized score 最高的 1 个。CONTROL 达到最终门槛写 `control_validation_pass=true`，但不构造 `SubmitReadyIssue`、不进入 discovery candidate ranking，也不进入主指标。

- [ ] **Step 7: 运行 validation/worker 集成测试**

Run: `.venv/bin/python -m pytest tests/integration/test_issue_validation.py tests/unit/validation/test_promotion.py tests/unit/test_runtime_first_worker.py -q`

Expected: PASS；测试验证两个不同 PID、两次 graph reload、pooled CI 和两条 submission path。

- [ ] **Step 8: 提交 Task 8**

```bash
git add src/graph_sym_perf/validation/session_runner.py src/graph_sym_perf/runtime/screening.py src/graph_sym_perf/validation/models.py src/graph_sym_perf/validation/validator.py src/graph_sym_perf/validation/promotion.py src/graph_sym_perf/experiment/worker.py tests/unit/validation/test_promotion.py tests/integration/test_issue_validation.py tests/unit/test_runtime_first_worker.py
git commit -m "feat: validate candidates through independent sessions"
```

### Task 9: 将 Neo4j BUG001 作为隔离 regression gate，而非新发现

**Files:**
- Create: `src/graph_sym_perf/validation/regression.py`
- Create: `tests/fixtures/regressions/neo4j_bug001/fast_query.cypher`
- Create: `tests/fixtures/regressions/neo4j_bug001/slow_query.cypher`
- Create: `tests/fixtures/regressions/neo4j_bug001/manifest.yaml`
- Modify: `tests/fixtures/regressions/manifest.yaml:1-3`
- Create: `tests/integration/test_regression_gate.py`
- Modify: `tests/unit/test_experiment_planner.py`

**Interfaces:**
- Consumes: 现有 `BUG list/neo4j/0_SYM_BUG001/reproducer_graph.cypher.txt`、独立 session validator、专用 regression Neo4j 环境变量。
- Produces: `RegressionCase`、`RegressionGateReport`、P0 可消费的 pass/fail 制品；永不产生 discovery accepted count 或 SubmitReadyIssue。

- [ ] **Step 1: 创建不含 PROFILE 的 regression query fixture**

`fast_query.cypher` 写入：

```cypher
MATCH (n2)
WITH *
WITH max('J') AS a0, n2
ORDER BY a0
WHERE (276190253 > -71315356)
MATCH (n0:L3)-[r4:T3]->(n6)<-[r5:T3]-(n7:L4), (n5)
WHERE (('3B' CONTAINS a0) AND ((r4.id) <> (r5.id)))
WITH DISTINCT (r4.k48) AS a1
WHERE a1
RETURN min('R') AS a2
ORDER BY a2
```

`slow_query.cypher` 仅把第二个 MATCH 的 comma patterns 改为 `(n5), (n0:L3)-...`。任何 fixture 中出现 `PROFILE` 或 `EXPLAIN` 都使测试失败。

- [ ] **Step 2: 写 regression manifest 环境与计数隔离测试**

```python
def test_bug001_manifest_requires_the_affected_version_and_image_digest(monkeypatch) -> None:
    monkeypatch.delenv("GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST", raising=False)
    with pytest.raises(RegressionConfigurationError, match="IMAGE_DIGEST"):
        load_regression_case("tests/fixtures/regressions/neo4j_bug001/manifest.yaml")


def test_regression_pass_never_counts_as_fresh_issue() -> None:
    report = RegressionRunner(_passing_validator()).run(_bug001_case())
    assert report.passed
    assert report.fresh_issue_count == 0
    assert report.discovery_accepted_pairs == 0
```

- [ ] **Step 3: 写 manifest 的可核查字段**

```yaml
schema_version: 1
case_id: neo4j_bug001_pattern_order
dbms: neo4j
affected_version: "2026.04.0 community"
image_ref: "neo4j:2026.04.0-community"
image_digest_env: GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST
uri_env: GRAPHSYMPERF_NEO4J_BUG001_URI
user_env: GRAPHSYMPERF_NEO4J_BUG001_USER
password_env: GRAPHSYMPERF_NEO4J_BUG001_PASSWORD
graph_path: ../../../../BUG list/neo4j/0_SYM_BUG001/reproducer_graph.cypher.txt
fast_query_path: fast_query.cypher
slow_query_path: slow_query.cypher
canonical_rule_id: pattern_order_symmetry
expected_graph_nodes: 1280
expected_graph_relationships: 2290
```

image digest 不写猜测值；doctor 从指定环境变量读取真实 digest 并冻结到 P0 regression manifest。缺失时 P0 明确阻断。

- [ ] **Step 4: 运行测试并确认 regression runner 不存在**

Run: `.venv/bin/python -m pytest tests/integration/test_regression_gate.py -q`

Expected: FAIL，缺少 `validation.regression`。

- [ ] **Step 5: 实现隔离 regression runner**

runner 必须：验证 affected version/image digest、使用独立 URI 创建 adapter、清空并加载固定图、核对 1280/2290、用与 discovery 相同的 independent validation protocol 复验 fixed pair、写 `regression_report.json`。受影响版本不复现则 `passed=false` 并阻断 P1；目标当前版本是否复现只作版本状态，不作为 P0 gate。

- [ ] **Step 6: 将 regression stream 加入 P0 RulePlan 但排除主预算**

P0 `RulePlan.entries` 包含一个 `stream=REGRESSION` entry，其 `accepted_pair_quota=0` 且引用 manifest case ID。`discovery_accepted_pair_budget` 和 `control_accepted_pair_budget` 都不包含该 entry；planner 测试显式断言。

- [ ] **Step 7: 运行 regression 和 planner 测试**

Run: `.venv/bin/python -m pytest tests/integration/test_regression_gate.py tests/unit/test_experiment_planner.py -q`

Expected: PASS；synthetic affected-version runner 可复现，fresh issue/accepted 计数始终为 0。

- [ ] **Step 8: 提交 Task 9**

```bash
git add src/graph_sym_perf/validation/regression.py tests/fixtures/regressions/manifest.yaml tests/fixtures/regressions/neo4j_bug001 tests/integration/test_regression_gate.py tests/unit/test_experiment_planner.py
git commit -m "test: gate campaign on known neo4j regression"
```

### Task 10: 强制 reducer 后独立复验并做保守 issue-family 去重

**Files:**
- Create: `src/graph_sym_perf/issues/dedup.py`
- Modify: `src/graph_sym_perf/reducer/models.py:11-35`
- Modify: `src/graph_sym_perf/reducer/reducer.py:11-52`
- Modify: `src/graph_sym_perf/validation/models.py`
- Modify: `src/graph_sym_perf/validation/promotion.py:12-25`
- Modify: `src/graph_sym_perf/issues/registry.py:10-27`
- Modify: `src/graph_sym_perf/issues/exporter.py:11-18`
- Modify: `tests/integration/test_runtime_preserving_reducer.py`
- Create: `tests/unit/issues/test_dedup.py`
- Modify: `tests/unit/issues/test_registry.py`
- Modify: `tests/unit/issues/test_exporter.py`

**Interfaces:**
- Consumes: original validation report、`CandidateReducer`、reduced pair 的第二次 independent validation、known regression signatures。
- Produces: `ReductionReport.reduced_validation`、`IssueFingerprint`、`IssueFamilyIndex`、带 `family_id` 的 `SubmitReadyIssue`。

- [ ] **Step 1: 写 reduced validation 必须独立通过的失败测试**

```python
def test_reduction_is_not_submit_ready_until_reduced_pair_is_independently_validated() -> None:
    report = _reduction_report(status=ReductionStatus.REDUCED_VALID, reduced_validation=None)
    assert promote(_original_validation_pass(), reduction=report, certificate=_core_certificate()) is None


def test_reduced_validation_failure_invalidates_reduction() -> None:
    report = validate_reduction(_reduction_report(), _validation_reject())
    assert report.status is ReductionStatus.REDUCTION_INVALIDATED
```

- [ ] **Step 2: 写 exact/structural 去重与不确定不合并测试**

```python
def test_exact_signature_includes_dbms_rule_pair_and_direction() -> None:
    left = fingerprint(_issue(query_a="MATCH (a:L0) RETURN a", query_b="MATCH (a:L0) RETURN a", direction="b_slower"))
    right = fingerprint(_issue(query_a="MATCH (a:L0) RETURN a", query_b="MATCH (a:L0) RETURN a", direction="a_slower"))
    assert left.exact != right.exact


def test_structural_merge_requires_path_and_reduced_phenotype_match() -> None:
    index = IssueFamilyIndex()
    first = index.add(_issue(path="relative", phenotype="stable"))
    second = index.add(_issue(path="absolute", phenotype="stable"))
    assert first.family_id != second.family_id
```

- [ ] **Step 3: 运行 reducer/dedup 测试并确认现有 promote 只看字符串状态**

Run: `.venv/bin/python -m pytest tests/integration/test_runtime_preserving_reducer.py tests/unit/issues/test_dedup.py tests/unit/validation/test_promotion.py -q`

Expected: FAIL，缺少 `reduced_validation` 与 `IssueFamilyIndex`。

- [ ] **Step 4: 扩展 reduction report 并执行 reduced validation**

```python
@dataclass(frozen=True)
class ReductionReport:
    status: ReductionStatus
    original_pair: EquivalentQueryPair
    reduced_pair: EquivalentQueryPair
    accepted_steps: tuple[ReductionStep, ...]
    rejected_steps: tuple[ReductionStep, ...]
    final_evidence: RuntimeEvidence
    reduced_validation: IssueValidationReport | None = None
```

reducer symptom predicate 仍只使用普通 execute runtime；reducer 结束后必须用 Task 8 的两个独立 process 对 reduced pair 再跑完整 validation。只有 `reduced_validation.stage=VALIDATION_PASSED` 且 submission direction 与原始 validation 一致时状态为 `REDUCED_VALID`。

- [ ] **Step 5: 实现 conservative fingerprint**

```python
@dataclass(frozen=True)
class IssueFingerprint:
    exact: str
    structural: str | None
    runtime_direction: str
    submission_path: str
    reduced_phenotype: str
```

exact payload 固定为 `dbms + canonical_rule_id + reduced_query_a_hash + reduced_query_b_hash + direction`。structural normalizer 只在 parser 能覆盖完整 query 时工作：alpha-renaming 按首次绑定顺序改为 `v0/v1`，literal 改为类型占位符，label/type/property name 改为各自稳定占位符；解析失败返回 `None`，不得 regex 猜测后自动合并。

- [ ] **Step 6: 实现 family merge 与 known-regression exclusion**

`IssueFamilyIndex.add()` 先 exact merge；structural merge 还要求 rule、site class、direction、submission path 和 reduced-validation phenotype 全相同。与 BUG001 regression signature 匹配的 fresh candidate 标记 `known_regression_match=true`，不进入主指标，也不能重新导出为新 issue。

- [ ] **Step 7: 收紧 promotion/export boundary**

`promote()` 接收完整 `ReductionReport` 和 `IssueFamilyDecision`，只有 CORE+submit eligible、original validation pass、`REDUCED_VALID`、reduced validation pass、非 known regression、已分配 family ID 时构造 `SubmitReadyIssue`。exporter 继续只接受 typed SubmitReadyIssue，并输出 exact/structural signature、submission path 和两个 validation report 的引用。

- [ ] **Step 8: 运行 reducer/issues 测试**

Run: `.venv/bin/python -m pytest tests/integration/test_runtime_preserving_reducer.py tests/unit/issues tests/unit/validation/test_promotion.py -q`

Expected: PASS；`REDUCTION_PARTIAL`、`REDUCTION_INVALIDATED`、known regression 或 uncertain structural signature 均不能自动 submit。

- [ ] **Step 9: 提交 Task 10**

```bash
git add src/graph_sym_perf/issues/dedup.py src/graph_sym_perf/reducer/models.py src/graph_sym_perf/reducer/reducer.py src/graph_sym_perf/validation/models.py src/graph_sym_perf/validation/promotion.py src/graph_sym_perf/issues/registry.py src/graph_sym_perf/issues/exporter.py tests/integration/test_runtime_preserving_reducer.py tests/unit/issues/test_dedup.py tests/unit/issues/test_registry.py tests/unit/issues/test_exporter.py tests/unit/validation/test_promotion.py
git commit -m "feat: deduplicate reduced validated issue families"
```

### Task 11: 实现 P1→P2 selector、phase manifest 与 fail-closed lifecycle

**Files:**
- Create: `src/graph_sym_perf/experiment/selection.py`
- Create: `src/graph_sym_perf/experiment/campaign.py`
- Modify: `src/graph_sym_perf/experiment/supervisor.py:19-112`
- Modify: `src/graph_sym_perf/experiment/lifecycle.py:16-65`
- Modify: `src/graph_sym_perf/experiment/planner.py`
- Modify: `src/graph_sym_perf/cli/runtime_first.py:16-82`
- Create: `tests/unit/test_experiment_selection.py`
- Modify: `tests/integration/test_experiment_lifecycle.py`
- Modify: `tests/integration/test_supervisor_recovery.py`
- Modify: `tests/integration/test_cli_experiment_lifecycle.py`

**Interfaces:**
- Consumes: P0/P1 `CellSummary`、CONTROL/regression reports、`RulePlan`。
- Produces: `PilotCellDecision`、`PilotSelection`、`select_p2_cells()`、phase-specific lifecycle 和 CLI phase chaining。

- [ ] **Step 1: 写 P1 hard gate 测试**

```python
@pytest.mark.parametrize(
    "summary, reason",
    (
        (_cell(typed_mismatches=1), "typed_equivalence_mismatch"),
        (_cell(accepted_pairs=199, quota=200), "underfilled"),
        (_cell(certificate_complete=False), "certificate_incomplete"),
        (_cell(health_events=1), "health_event"),
        (_cell(artifact_complete=False), "artifact_incomplete"),
        (_cell(terminal_state="resource_exhausted"), "resource_exhausted"),
    ),
)
def test_p1_hard_gate_is_fail_closed(summary: CellSummary, reason: str) -> None:
    decision = evaluate_p1_cell(summary)
    assert not decision.eligible
    assert reason in decision.reason_codes


def test_p0_gate_requires_core_control_and_regression_conditions() -> None:
    report = evaluate_p0_gate(
        core_cells=(_cell(accepted_pairs=50, quota=50),),
        control_cells=(_control_cell(control_validation_passed_count=0),),
        regression=_regression_report(passed=True),
    )
    assert report.passed
```

- [ ] **Step 2: 写 max-6、family 保留和双规模例外测试**

```python
def test_selector_keeps_validation_winners_and_at_most_six_cells() -> None:
    selection = select_p2_cells(_pilot_fixture(), max_cells=6)
    assert len(selection.cells) <= 6
    assert all(cell.validation_passed_count > 0 for cell in _mandatory_winners(selection))
    assert len([cell for cell in selection.cells if cell.family == "bool" and cell.validation_passed_count == 0]) <= 1
    assert len([cell for cell in selection.cells if cell.family == "sym" and cell.validation_passed_count == 0]) <= 1


def test_two_scales_for_one_rule_require_independent_validation_passes() -> None:
    selection = select_p2_cells(_same_rule_two_scale_fixture(one_pass=False), max_cells=6)
    assert len([cell for cell in selection.cells if cell.rule_id == "pattern_order_symmetry"]) == 1
```

- [ ] **Step 3: 运行 selector 测试并确认模块不存在**

Run: `.venv/bin/python -m pytest tests/unit/test_experiment_selection.py -q`

Expected: FAIL，缺少 `experiment.selection`。

- [ ] **Step 4: 定义 CellSummary 和确定性排序**

```python
@dataclass(frozen=True)
class CellSummary:
    rule_id: str
    family: str
    stream: str
    graph_scale: int
    shard_seed: int
    accepted_pairs: int
    quota: int
    applicability_count: int
    stage2_count: int
    validation_passed_count: int
    control_validation_passed_count: int
    validation_pair_yield: float
    validation_yield_ci_low: float
    best_validated_delta_ms: float
    best_validated_ratio: float
    best_ratio_ci_low: float
    best_delta_over_noise: float
    typed_mismatches: int
    certificate_complete: bool
    health_events: int
    artifact_complete: bool
    timeout_rate: float
    terminal_state: str
```

eligible cells 的排序 key 固定为：validation pass presence、yield CI low、validated delta、validated ratio、ratio CI low、delta/noise、applicability、较低 timeout rate、rule ID、scale。所有 validation winner 先入选；若 winner 超过 6，selector 必须返回 `selection_overflow` 并要求人工修改预注册 max，而不是静默丢 winner。

- [ ] **Step 5: 实现 CONTROL 全局 gate 和 `pilot_selection.json`**

任一 P1 CONTROL `control_validation_pass>0` 时整个 P2 selection 失败。`PilotSelection` 记录所有 cell 的 eligible/reason、排序分数、被选/未选原因、输入 P1 manifest hash 和 selector protocol version；写入后不可覆盖。

- [ ] **Step 6: 让 supervisor 按 RulePlanEntry 终态分类**

supervisor 读取 `entry.accepted_pair_quota` 和 worker 返回的 `terminal_state`；只有 quota 精确达到且 terminal=`completed` 才进入 completed。`underfilled/quarantined/resource_exhausted/infrastructure_failed` 分开计数并写 append-only event；retry 创建新 attempt 目录，不覆盖旧证据。

- [ ] **Step 7: 实现 phase lifecycle 和单一 CLI 入口**

CLI 增加：

```text
graphsymperf experiment plan --config ... --phase p0|p1|p2 [--selection ...]
graphsymperf experiment run --config ... --phase p0|p1|p2 --run-id ... [--parent-run ...] [--selection ...]
graphsymperf experiment select --config ... --p1-run ... --output ...
graphsymperf experiment report --config ... --campaign-root ...
```

P0 只有全部 active CORE quota 达标、零 mismatch/certificate gap/health event、CONTROL validation pass 为 0 且 affected-version regression 通过时才写 `p0_gate.passed=true`。P1 要求一个成功 P0 parent manifest；P2 要求成功 P0/P1 ancestry 和 `pilot_selection.json`。每个 phase 在运行前写一次 `rule_plan.json` 与 `resolved_manifest.json`，存在即拒绝覆盖；每个 entry 必须写 `cell_summary.json`。`campaign.py` 只协调 phase/ancestry，不复制 worker 逻辑。

campaign 在进入 discovery、Stage-2 或 validation 前获取 campaign-root 下的 Neo4j exclusive measurement lock；同一时刻只允许一个 measurement session，validation 子进程运行时 discovery 保持暂停。并发获取锁的集成测试必须得到 `MEASUREMENT_SESSION_BUSY`，不能并行共享目标实例。

- [ ] **Step 8: 运行 selector/lifecycle/CLI 测试**

Run: `.venv/bin/python -m pytest tests/unit/test_experiment_selection.py tests/integration/test_experiment_lifecycle.py tests/integration/test_supervisor_recovery.py tests/integration/test_cli_experiment_lifecycle.py -q`

Expected: PASS；P2 plan 恰为 `selected cells × 3 shards × 1000`，且所有 P2 seed 与 P1 不重叠。

- [ ] **Step 9: 提交 Task 11**

```bash
git add src/graph_sym_perf/experiment/selection.py src/graph_sym_perf/experiment/campaign.py src/graph_sym_perf/experiment/supervisor.py src/graph_sym_perf/experiment/lifecycle.py src/graph_sym_perf/experiment/planner.py src/graph_sym_perf/cli/runtime_first.py tests/unit/test_experiment_selection.py tests/integration/test_experiment_lifecycle.py tests/integration/test_supervisor_recovery.py tests/integration/test_cli_experiment_lifecycle.py
git commit -m "feat: orchestrate staged validated yield campaign"
```

### Task 12: 计算主/诊断 yield、置信区间和证据报告

**Files:**
- Create: `src/graph_sym_perf/reporter/yield_report.py`
- Modify: `src/graph_sym_perf/reporter/__init__.py`
- Modify: `src/graph_sym_perf/experiment/campaign.py`
- Create: `tests/unit/reporter/test_yield_report.py`
- Create: `tests/integration/test_validated_yield_campaign.py`

**Interfaces:**
- Consumes: P2 cell summaries、validation reports、reduction reports、IssueFamilyIndex 和 phase manifests。
- Produces: `YieldReport`、`yield_report.json/md`、`issue_families.json`、`claim_evidence_table.md`、Wilson CI 和 shard-block bootstrap CI。

- [ ] **Step 1: 写两种 yield 分母隔离测试**

```python
def test_yield_excludes_control_regression_and_failed_entries() -> None:
    report = build_yield_report(
        entries=(
            _entry_summary(stream="discovery", terminal="completed", accepted=1000, validation_passed=2, families=("F1",)),
            _entry_summary(stream="control", terminal="completed", accepted=50, validation_passed=1, families=()),
            _entry_summary(stream="regression", terminal="completed", accepted=0, validation_passed=1, families=("KNOWN",)),
            _entry_summary(stream="discovery", terminal="infrastructure_failed", accepted=400, validation_passed=0, families=()),
        )
    )
    assert report.discovery_accepted_pairs == 1000
    assert report.submit_ready_unique_families == 1
    assert report.submit_ready_unique_yield_per_1k == 1.0
    assert report.validation_pair_yield_per_1k == 2.0
```

- [ ] **Step 2: 写 Wilson 与 blocked bootstrap 的确定性测试**

```python
def test_zero_hit_report_keeps_budget_and_nonzero_upper_bound() -> None:
    report = build_yield_report((_entry_summary(stream="discovery", terminal="completed", accepted=3000, validation_passed=0, families=()),))
    assert report.submit_ready_unique_yield_per_1k == 0.0
    assert report.validation_pair_ci.low == 0.0
    assert report.validation_pair_ci.high > 0.0


def test_family_bootstrap_resamples_whole_shards() -> None:
    interval = bootstrap_unique_family_yield(_three_shard_family_fixture(), resamples=10000, seed=20260714)
    assert interval.low <= interval.high
    assert interval.method == "shard_block_bootstrap"
```

- [ ] **Step 3: 运行 reporter 测试并确认模块不存在**

Run: `.venv/bin/python -m pytest tests/unit/reporter/test_yield_report.py -q`

Expected: FAIL，缺少 `reporter.yield_report`。

- [ ] **Step 4: 定义 YieldReport 与统计口径**

```python
@dataclass(frozen=True)
class YieldReport:
    discovery_accepted_pairs: int
    independent_validation_passed_pairs: int
    submit_ready_unique_families: int
    validation_pair_yield_per_1k: float
    submit_ready_unique_yield_per_1k: float
    validation_pair_ci: ConfidenceInterval
    unique_family_yield_ci: ConfidenceInterval
    diagnostics: Mapping[str, object]
```

Wilson 95% CI 只用于 pair-level passed/accepted；unique family yield 用 shard/seed 为 block 的 10000 次 bootstrap。failed/quarantined/infrastructure entries 在主分母外单列，不得把其 partial accepted count 混入 completed denominator。

- [ ] **Step 5: 输出所有必需制品**

`write_yield_artifacts()` 原子写：

- `yield_report.json`
- `yield_report.md`
- `issue_families.json`
- `claim_evidence_table.md`
- `regression_report.md`

phase worker 还必须已生成 `rule_plan.json`、每 cell 的 `pair_runtime.jsonl/cell_summary.json`、`validation/` 与 `reduction/`；reporter 在这些输入缺失时 fail-closed，不能生成部分 `yield_report`。

报告按总体、BOOL/SYM、rule、graph scale、shard seed 展开 applicability、accepted、Stage-1、Stage-2、validation precision、reducer survival、duplicate rate、timeout rate 和每 1000 accepted pairs 成本；零命中行不能省略。

- [ ] **Step 6: 写 fake campaign 集成测试**

fake campaign 使用 2 个 CORE、1 个 CONTROL、三规模小 quota，构造一个 relative submit-ready family、一个 absolute submit-ready family、一个 exact duplicate 和一个 known regression match。断言主分子为 2、diagnostic validation pair 数包含 duplicate 但不含 CONTROL/regression、所有必需文件存在且与 JSON 计数一致。

- [ ] **Step 7: 运行 reporter 和 fake campaign 测试**

Run: `.venv/bin/python -m pytest tests/unit/reporter/test_yield_report.py tests/integration/test_validated_yield_campaign.py -q`

Expected: PASS；Markdown 中明确以 submit-ready unique family yield 为主指标，以 validation pair yield 为诊断指标。

- [ ] **Step 8: 提交 Task 12**

```bash
git add src/graph_sym_perf/reporter/yield_report.py src/graph_sym_perf/reporter/__init__.py src/graph_sym_perf/experiment/campaign.py tests/unit/reporter/test_yield_report.py tests/integration/test_validated_yield_campaign.py
git commit -m "feat: report validated runtime yield"
```

### Task 13: 完成端到端 fail-closed 验收和操作文档

**Files:**
- Create: `docs/neo4j-validated-runtime-yield.md`
- Modify: `README.md`
- Modify: `src/graph_sym_perf/experiment/doctor_v2.py:62-109`
- Modify: `tests/unit/test_runtime_first_doctor.py`
- Modify: `tests/contracts/test_layer_boundaries.py`
- Modify: `tests/integration/test_validated_yield_campaign.py`

**Interfaces:**
- Consumes: 全部前置任务接口。
- Produces: 可复制的 P0/P1/P2 命令、doctor 环境检查、架构边界与最终测试证据。

- [ ] **Step 1: 写 doctor 缺失 regression 环境与磁盘预算测试**

```python
def test_campaign_doctor_refuses_missing_bug001_image_digest(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST", raising=False)
    report = run_doctor("config/neo4j_validated_runtime_yield.yaml", output_root=tmp_path)
    assert not report.ready
    assert "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST" in report.missing_requirements


def test_campaign_doctor_budgets_only_the_requested_phase(monkeypatch, tmp_path) -> None:
    report = run_doctor("config/neo4j_validated_runtime_yield.yaml", phase="p1", output_root=tmp_path)
    assert report.disk_required_bytes == report.rule_plan_entry_count * 500 * 1024 * 1024
```

- [ ] **Step 2: 增加层边界 contract**

```python
def test_worker_does_not_import_profile_or_adaptive_sampler() -> None:
    source = Path("src/graph_sym_perf/experiment/worker.py").read_text(encoding="utf-8")
    assert "profile(" not in source
    assert "AdaptiveRuleSampler" not in source
    assert "and_excluded_middle_or_null" not in source
```

- [ ] **Step 3: 运行 doctor/contract 测试并确认缺少新字段**

Run: `.venv/bin/python -m pytest tests/unit/test_runtime_first_doctor.py tests/contracts/test_layer_boundaries.py -q`

Expected: FAIL，doctor 尚未报告 regression env 或 phase-specific budget。

- [ ] **Step 4: 实现 doctor fail-closed 检查**

doctor 对指定 phase 检查：schema/Registry audit、GDsmith clean commit、Neo4j driver/connectivity、目标数据库隔离、rule capabilities、真实 BUG001 image digest/URI、entry 级磁盘预算、query timeout 和 max entry runtime。所有检查结果写 `doctor.json`；任一缺失返回 CLI exit 4。

- [ ] **Step 5: 编写操作文档**

`docs/neo4j-validated-runtime-yield.md` 必须包含：

```bash
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment plan --config config/neo4j_validated_runtime_yield.yaml --phase p0 --output /tmp/neo4j-vry-p0-plan.json
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment doctor --config config/neo4j_validated_runtime_yield.yaml --phase p0 --output-root runs
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p0 --run-id neo4j-vry-p0-20260714 --output-root runs
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p1 --run-id neo4j-vry-p1-20260714 --parent-run runs/neo4j-vry-p0-20260714 --output-root runs
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment select --config config/neo4j_validated_runtime_yield.yaml --p1-run runs/neo4j-vry-p1-20260714 --output runs/neo4j-vry-p1-20260714/pilot_selection.json
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p2 --run-id neo4j-vry-p2-20260714 --parent-run runs/neo4j-vry-p1-20260714 --selection runs/neo4j-vry-p1-20260714/pilot_selection.json --output-root runs
.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment report --config config/neo4j_validated_runtime_yield.yaml --campaign-root runs --p2-run runs/neo4j-vry-p2-20260714
```

文档同时说明：P0 失败不能继续；P1 selection overflow/control pass 不能继续；已知 BUG001 不进入 fresh yield；零命中必须保留 CI，不能改门槛后复用同一 campaign ID。

- [ ] **Step 6: 运行相关完整测试矩阵**

Run: `.venv/bin/python -m pytest tests/unit/runtime tests/unit/transforms tests/unit/validation tests/unit/issues tests/unit/reporter tests/unit/test_experiment_spec.py tests/unit/test_experiment_planner.py tests/unit/test_experiment_selection.py tests/unit/test_runtime_first_worker.py tests/property tests/contracts tests/integration/test_validated_yield_campaign.py tests/integration/test_issue_validation.py tests/integration/test_runtime_preserving_reducer.py tests/integration/test_regression_gate.py -q`

Expected: PASS，无 skip 掩盖 validated-yield 核心路径。

- [ ] **Step 7: 运行全套测试并与既有基线比较**

Run: `.venv/bin/python -m pytest tests -q`

Expected: 除 Global Constraints 已登记的两个 AgensGraph/Apache AGE 参数实例外无失败；若这两个既有失败已由其他工作修复，则全绿。任何新增失败都阻断提交。

Run: `.venv/bin/python -m compileall -q src tests`

Expected: exit 0。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 8: 提交 Task 13**

```bash
git add docs/neo4j-validated-runtime-yield.md README.md src/graph_sym_perf/experiment/doctor_v2.py tests/unit/test_runtime_first_doctor.py tests/contracts/test_layer_boundaries.py tests/integration/test_validated_yield_campaign.py
git commit -m "docs: document validated yield campaign"
```

### Task 14: 执行 Neo4j P0、P1 与入选 P2，并冻结实验结论

**Files:**
- Generated only: `runs/neo4j-vry-p0-20260714/`
- Generated only: `runs/neo4j-vry-p1-20260714/`
- Generated only: `runs/neo4j-vry-p2-20260714/`

**Interfaces:**
- Consumes: Task 13 文档中的固定命令和真实 Neo4j/GDsmith 环境。
- Produces: live P0/P1/P2 artifacts、`pilot_selection.json`、`yield_report.json/md`、最多 3 个首选 developer-facing issue family evidence packs。

- [ ] **Step 1: 运行 P0 doctor**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment doctor --config config/neo4j_validated_runtime_yield.yaml --phase p0 --output-root runs`

Expected: exit 0，`doctor.json.ready=true`；若真实 BUG001 image digest、隔离 URI、磁盘或 GDsmith clean commit 缺失，停止并报告 blocker，不得绕过。

- [ ] **Step 2: 运行 P0 并核验 gate**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p0 --run-id neo4j-vry-p0-20260714 --output-root runs`

Expected: 每条 active CORE 在 128 达到 50 accepted；两个 P0 CONTROL 无 validation pass；BUG001 在 manifest 指定 affected version 上复现；无 mismatch、health event、artifact gap 或非终态 entry。

- [ ] **Step 3: 运行 P1 pilot**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p1 --run-id neo4j-vry-p1-20260714 --parent-run runs/neo4j-vry-p0-20260714 --output-root runs`

Expected: active CORE 的 128/256/512 cell 各 200 accepted，CONTROL 三规模各 50 accepted；所有 cell summary 可审计。

- [ ] **Step 4: 冻结 P2 selection 并人工审计 hard gate**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment select --config config/neo4j_validated_runtime_yield.yaml --p1-run runs/neo4j-vry-p1-20260714 --output runs/neo4j-vry-p1-20260714/pilot_selection.json`

Expected: 最多 6 个 cell；所有 validation winner 保留；BOOL/SYM 零 pass 探索 cell 各最多 1 个；无 CONTROL pass。检查 selection 输入 manifest hash 与 P1 完全一致后才继续。

- [ ] **Step 5: 运行 P2 的 3×1000 fresh shards**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment run --config config/neo4j_validated_runtime_yield.yaml --phase p2 --run-id neo4j-vry-p2-20260714 --parent-run runs/neo4j-vry-p1-20260714 --selection runs/neo4j-vry-p1-20260714/pilot_selection.json --output-root runs`

Expected: 每个入选 cell 恰有 3 个 completed shards，每 shard 1000 compact runtime records；underfilled/failed cell 不伪装 completed。

- [ ] **Step 6: 生成报告并选 top 3 issue families**

Run: `.venv/bin/python -m graph_sym_perf.cli.runtime_first experiment report --config config/neo4j_validated_runtime_yield.yaml --campaign-root runs --p2-run runs/neo4j-vry-p2-20260714`

Expected: 报告同时给出主/诊断 yield、CI 和完整漏斗。Top 3 只从 fresh、非 known-regression、`REDUCED_VALID` unique families 中选择，依次按 submission path 强度、absolute delta、ratio CI low、delta/noise 排序；不足 3 个时如实报告实际数量，禁止用 CONTROL、duplicate、partial reduction 或 lowered threshold 补齐。

- [ ] **Step 7: 冻结 claim-evidence table，不提交 generated runs 到 Git**

检查 `claim_evidence_table.md` 中每个数字能追溯到 phase manifest、pair runtime、validation、reduction 和 family record。`runs/` 保持未跟踪，不执行 Git commit；对外 issue 文本另起审核任务，不能由本实验任务自动发布。

## Spec Coverage Matrix

| 设计规格要求 | 实施任务 |
|---|---|
| 主/诊断 validated runtime yield | Task 12、14 |
| Registry 唯一事实源、CORE/CONTROL/EXPERIMENTAL 隔离 | Task 1、3、5 |
| `pattern_order_symmetry` 安全边界 | Task 2、9 |
| 每规则独立预算和统计 | Task 3、5、11 |
| GDsmith 128/256/512 `--max-node-num` | Task 4 |
| 每 accepted pair 紧凑 runtime 摘要 | Task 6 |
| A/A 噪声、Stage-1 三队列、Stage-2 宽召回 | Task 6、7 |
| 双独立 session、relative OR absolute final gate | Task 8 |
| regression 不计新发现、CONTROL 校准 | Task 8、9、11 |
| reducer、reduced validation、family dedup | Task 10 |
| P1 hard gate、最多 6 cell、P2 3×1000 | Task 11、14 |
| yield JSON/Markdown、Wilson/blocked bootstrap、零命中纪律 | Task 12 |
| fail-closed lifecycle、doctor、测试与复现命令 | Task 5、11、13、14 |

## Completion Evidence

计划实施完成只允许声明以下分层结论：

1. **工程完成：** 相关测试矩阵通过、无新增全套失败、P0/P1/P2 manifests 与 record 数量一致。
2. **实验可信：** P0 regression/control gate 通过，P1 selection 可复算，P2 全部入选 cell 有三组 fresh seeds。
3. **发现结果：** 仅根据 `yield_report.json` 报告主/诊断 yield 和 CI；`yield=0` 是允许的可信结果。
4. **可提交 issue：** 只列 fresh `REDUCED_VALID` unique families；known BUG001、CONTROL、duplicate 和人工审查中的 partial reduction 不得计入。

# GraphSymPerf：随机 AST 与 Pattern 嵌入的代码技术设计

方法版本：`ast-pattern-v1`；文档修订：`gdsmith-r1`。日期：2026-10-26。配套方法文档：[随机 AST 与 Pattern 嵌入的方法](ast-pattern-method.md)。

本文规定新的实现目标，以及现有代码到该目标的迁移方式。六个阶段统一标识为 M1–M6：随机生成 AST、嵌入模式（Pattern）、构造模式感知图状态（Pattern-aware Graph State）、实例化完整查询、执行局部变异、检查依赖关系。本文中的新接口、目录和记录格式均为**拟议设计（proposed）**；本轮只编写文档，没有执行代码重构或数据库实验。现状表依据 2026-10-26 工作区源码静态核查，不以测试文件的存在推断测试已经通过。

## 1. 实现目标与不可混同的对象

新入口必须先生成与具体 Pattern 无关的抽象语法树（Abstract Syntax Tree，AST），再从树中选择兼容位置嵌入 Pattern。原有“选择 Pattern → 构造内核 → 向外包装”的入口退出新生成路径。现有类型系统、作用域分析、局部变异规则、显式图表示、构造器和执行器可以按接口复用。

区分下列对象，避免同一个 `pattern` 字段承担多种职责。

| 对象 | 具体含义 | 不承担的职责 |
|---|---|---|
| 宿主 AST `HostAst` | M1 生成的查询结构、类型、符号槽位与嵌入端口 | 不选择 Pattern ID 或按 Pattern 生成外壳 |
| 规则 `PatternSpec` | 已定义的局部结构、适用前提、图约束、变异和性能测试目标 | 不返回完整宿主查询或固定完整图配方 |
| 嵌入实例 `PatternInstance` | 某规则在某棵树某个词法位置的一次使用 | 不把相同变量拼写视为相同绑定 |
| 图状态 `GraphState` | 确定的节点、边、属性、索引及可核查事实 | 不只记录 Pattern 名称与规模 |
| 联合绑定方案 `BindingPlan` | 完整原查询与声明的变异备选所需的全部终端绑定 | 不在实例化时继续随机长出算子 |
| 查询对 `QueryPair` | 同一图状态上，一个基准查询及一次指定局部变异 | 不同时改变多个目标位点或图状态 |
| 依赖报告 `DependencyReport` | 静态依赖、目标是否暴露及运行时证据 | 不把语义不相关等同于执行时零工作 |

本文将预定义规则称为 Pattern，将 `ir.Node/Edge` 等 Cypher 匹配结构称为“图匹配项”。新增命名避免沿用二者共用 `Pattern` 的歧义。

六阶段共享以下约束：完整结构在 M2 结束时确定；M3 联合检查原结构与已声明变异备选；M4 只填充终端；M5 只应用一个登记的变异；M6 分别报告等价性、逻辑需求、调用多重性和物理执行证据。任一阶段失败均产生可回放的失败记录。

## 2. 当前代码的已核查状态

### 2.1 实际入口与路线差异

| 当前代码位置 | 已观察行为 | 新路线中的处理 |
|---|---|---|
| `src/main.py:311`，`prepare_case` | 调用 `instantiate`；缺少 `instantiation_record` 时执行 `mutate`；最后保存 IR 并编译 | 改为 M1–M6 显式编排；不根据某字段是否存在隐式选择路线 |
| `src/generator.py:1990`，`instantiate` | 接受 Pattern；`:2026` 调用 `abstract_seed`；`:2037` 调用 `_BUILDERS[pattern]` | 保留旧记录回放能力；退出新生成入口 |
| `src/generator.py:2070`，`_instantiate_composed` | 顺次调用 `build_symbolic_skeleton`、`construct_state`、`instantiate_pair` | 不能直接更名为新六阶段实现 |
| `src/symbolic.py:263`，`build_symbolic_skeleton` | 按 Pattern 构造局部 kernel；`:386` 才调用 `compose_fragment` 包装外层 | 拆出可复用局部规则；不保留为新宿主生成器 |
| `src/composition.py:96`，`compose_fragment` | 从已有 kernel 向外添加 `exists/count/with` | 提取算子的输入输出语义；不以该包装顺序实现 M1 |
| `src/symbolic.py:444`，`construct_state` | 先绑定候选语法，再依据全树与变异备选构造状态 | 复用求解组件，改为 M3 的联合状态与绑定合同 |
| `src/symbolic.py:572`，`instantiate_pair` | 校验构造记录，实例化 source；`:639` 已调用 `mutate` | 拆成 M4 与 M5，分别保存记录 |
| `src/dependencies.py:1361`，`propagate_equivalence` | 沿实际祖先传播路径双射、标量相等和结果多重集相等 | 保留为语义证明组件，不能直接充当性能传播检查 |
| `src/main.py:270`，`compile_case`；`src/executor.py:729`，`run_case` | 目标编译、状态和结果校验、计时及确认流程 | 通过明确适配层接入 M6 的运行时部分 |

组合支路对外部种子还在 `src/generator.py:2085` 记录 `provenance_only_not_embedded`，所以不能把现状写成“在任意输入查询 AST 中嵌入 Pattern”。

### 2.2 可复用能力与验证边界

| 能力 | 源码证据 | 复用方式与边界 |
|---|---|---|
| 有类型 IR 与作用域 | `src/ir.py:23–213`；`row_stream_imports:467`、`row_stream_schema:478`、`validate_query:694` | 复用节点类型和校验器；抽象槽位校验需要补充 |
| 行流和标量语法 | `src/row_grammar.py:22`；`src/scalar_grammar.py:24` | 提取通用产生式；移除宿主生成对特定 law 的依赖 |
| 结构位置与词法绑定 | `src/dependencies.py:21`、`:36`、`:106`、`:191`、`:207` | 复用定位、替换、定义—使用与行消费记录 |
| 显式变异标记 | `src/symbolic.py:119`，`MutationMark` | 扩展为多个登记实例、一个目标实例；位置变化需来源映射 |
| 显式图表示 | `src/state.py:87`，`GraphSpec`；`:114–125`、`:150–152` | 复用 v2 图组；v1 Pattern 配方仅用于旧回放 |
| 图需求与求解 | `src/population.py:29–110`、`:367`；`src/requirements.py:1570` | 复用 Match/Path/Value/Binding 等需求，加入端口、规则实例和终端槽位来源 |
| 局部变异与节点来源 | `src/mutator.py:641`、`:646`、`:661`、`:744` | 将规则派发收敛到 Pattern 注册表，保留语义守卫 |
| 小图解释器 | `src/population.py:411`，`evaluate_query` | 用于构造检查和反例；实际目标语义仍须数据库核验 |
| 全图增长观察闭合 | `src/relational_population.py:915–937` | 复用增长图域与读取位置的关系；其结果不是执行时工作量证明 |
| 结果与计时 | `src/executor.py:538`、`:577`、`:583`、`:729` | 复用完整有类型结果多重集比较、AB/BA 调度和独立确认 |

当前 `src/main.py:282–285` 与 `src/executor.py:765–766` 对 GraphSpec v2 的非 Neo4j 加载明确返回不支持。新路线应先贯通 Neo4j；其他目标必须分别完成语法、图加载、语义及计划证据适配后才能声明支持。旧后端支持列表不自动成为新路线的支持列表。

### 2.3 GDsmith 的采用机制与落点

本次借鉴依据为 Ziyue Hua、Wei Lin、Luyao Ren、Zongyang Li、Lu Zhang、Wenpin Jiao、Tao Xie，2023，*GDsmith: Detecting Bugs in Cypher Graph Database Engines*，ISSTA 2023，pp. 163–174，[DOI](https://doi.org/10.1145/3597926.3598046)。[正式论文 §3.2.1–3.2.3、Table 1、Algorithms 2–3](https://taoxiease.github.io/publications/issta23-gdsmith.pdf) 将子句骨架、图引导的匹配结构和数据引导的表达式生成分开组织。本文选择采用子句骨架、有类型递归展开、变量生命周期与需求分解机制；GDsmith 先有图再生成匹配与条件的次序不移入 GraphSymPerf 的 M1。

源码核查固定在公开提交 [`cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217`](https://github.com/ddaa2000/GDsmith/commit/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217)。下表源码路径均位于该提交的 `src/main/java/org/example/gdsmith/cypher/` 下；它们是设计来源，不是本仓库已引入的依赖。

| GDsmith 来源 | 已核查机制 | GraphSymPerf 采用位置与调整 |
|---|---|---|
| [`gen/query/ModelBasedQueryGenerator.java:18–33,54–68`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/query/ModelBasedQueryGenerator.java#L18) | 递归选择合法后继子句；先形成序列，再调用 QueryFiller | M1 内先生成通用子句骨架，再展开宿主结构；源码中固定初始 MATCH 不作为本系统要求，允许既有 `RETURN COUNT { … }` 宿主 |
| [`dsl/QueryFiller.java:37–74`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/dsl/QueryFiller.java#L37) | MATCH 先补匹配后补条件，WITH 先补别名后补条件 | M1 的结构展开按依赖顺序进行；原 QueryFiller 会生成结构，不能作为 M4 的终端实例化实现 |
| [`standard_ast/Symtab.java:163–195,244`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/standard_ast/Symtab.java#L163)；[`With.java:19–20`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/standard_ast/With.java#L19) | 子句符号表按匹配定义与投影导出组织变量可见性 | M1 维护显式输入/输出作用域、别名来源和可空性；依照本系统支持的 IR 规则处理 WITH，不复制名称列表充当绑定身份 |
| [`gen/expr/NonEmptyExpressionGenerator.java:263–264,291–342,358–425`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/expr/NonEmptyExpressionGenerator.java#L263) | 以期望类型筛选变量；条件携带 BooleanAssertion；递归构造表达式 | 类型驱动的结构展开进入 M1；期望值分解进入 M3。M3 只分析已经固定的算子，不调用该生成器重新选择结构；按三值真值表独立实现与验证，包括 NOT 的目标值取反 |
| [`gen/query/GraphGuidedQueryGenerator.java:18–44`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/query/GraphGuidedQueryGenerator.java#L18) | 持有 GraphManager，匹配/条件/别名生成共享 `varToProperties` | M1 禁止读取具体图与该类值表；M3 建立基于实际绑定域的联合值约束，不能以一个映射值替代所有输入的事实 |
| [`gen/pattern/SlidingPatternGenerator.java:47–90,194–198`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/pattern/SlidingPatternGenerator.java#L47) | 从已有图区域取得子图、转为匹配项并记录属性信息 | 借鉴实体映射与共享变量的显式记录；不在 M1 从图抽取结构。这里的 graph pattern 是 Cypher 图匹配项，不是 P3/S7 等预定义测试 Pattern |

GDsmith 的 GenExpr 同时选择结构并满足值约束。本系统将两项职责分到 M1 与 M3，分别冻结结构和求解图/终端绑定。M4 只物化已接受终端；GDsmith 的“完成骨架”不能直接对应本系统的“实例化查询”。

## 3. 拟议模块划分与依赖方向

下列路径是拟新增模块，不表示文件已经存在。设计优先复用现有 IR，不另建一套含义不同的查询语言。

```text
src/ast_pattern/
    contracts.py           # 六阶段记录、失败类型、稳定标识
    host_generator.py      # M1：子句骨架，再有类型递归展开宿主结构
    pattern_registry.py    # PatternSpec 注册、版本、局部合同
    embedding.py           # M2：端口选择、绑定接入、局部结构插入
    state_builder.py       # M3：全树约束、联合绑定、图构造适配
    instantiation.py       # M4：全树终端填充、完整性与不漂移校验
    mutation.py            # M5：指定实例的局部变异与来源追踪
    dependency_check.py    # M6：静态传播和跨 Pattern 影响
    evidence_adapter.py    # 数据库结果、计划、运行时证据适配
    pipeline.py            # 编排、预算、失败记录与回放
```

依赖方向为 `pipeline → 六阶段模块 → contracts/现有 IR 与语义组件`。`host_generator` 不导入 `pattern_registry`，不读取 PatternSpec、Pattern 清单、GraphManager、具体 Graph State 或运行时值表 Vals，不调用 `_BUILDERS` 或 `build_symbolic_skeleton`。它只读取语法、抽象类型约束、目标语言能力和预算。Pattern 模块可以调用通用局部产生式，但不能调用完整宿主生成器。数据库适配器不能反向修改 AST、绑定或 Graph State。

实现时可以先将适配器置于现有目录，不要求一次性搬迁文件；必须先形成上述接口边界和测试，再移动代码。禁止仅将现有条件分支搬到新文件后宣称消除硬编码。

## 4. 数据合同

### 4.1 宿主、端口与嵌入记录

| 拟议记录 | 必需字段 | 校验要点 |
|---|---|---|
| `ClauseSkeleton` | `clauses`（各含 ID、kind、syntax_slots）、`transition_trace`、`skeleton_seed`、`skeleton_digest` | M1 内部产物；记录子句序列与允许的语法位置，不能直接送入 M2 |
| `ScopeState` | `scope_id`、`parent_scope`、`visible_symbols`、`definitions`、`exports`、`alias_sources`、`nullable_types` | 不包含实体取值；名称、类型与词法身份分别保存 |
| `ExpansionRequest` | `syntax_category`、`expected_type`、`nullable_allowed`、`scope_id`、`address`、`remaining_budget` | M1 按期望类型展开结构；不接受期望实际图值或 PatternSpec |
| `HostAst` | `tree`、`node_ids`、`slots`、`ports`、`scope_graph`、`clause_skeleton_digest`、`expansion_trace`、`grammar_version`、`host_seed`、`shape_digest` | 子句、匹配和表达式的算子结构均已展开；只允许声明的图相关终端槽位和 Pattern 结构端口未填 |
| `TerminalSlot` | `slot_id`、`kind`、`type`、`owner_scope`、`domain`、`shared_with` | 区分标签、类型、属性键、字面量、参数；共享关系由显式 ID 表示 |
| `EmbeddingPort` | `port_id`、`address`、`input_schema`、`output_schema`、`visible_symbols`、`observation_contract`、`reserved_budget` | 行流、标量、谓词、图匹配项不能混插；输出多重性、可空性及路径观察属于合同 |
| `PatternInstance` | `instance_id`、`pattern_id`、`form`、`rule_version`、`port_id`、`site_ids`、`imports`、`exports`、`terminal_slots`、`declared_alternative` | 原结构与变异备选在 M2 均已声明；导入绑定使用词法 symbol ID |
| `EmbeddedAst` | `host_digest`、`tree`、`instances`、`target_id`、`origin_map`、`outside_port_digest` | 端口之外的算子和符号来源保持不变；无剩余结构端口 |

M1 的结构端口是带接口的语法位置，不是“等生成图后再随意长出的空洞”。首版规定宿主只含一个未填结构端口，其余所有子树在 M1 已完成。多端口扩展在 M1 为每个候选端口生成默认完整子树，并将其作为规范基线（canonical baseline）归档；M2 选择的端口被登记实例替换，未选择的端口直接保留默认子树，不再次采样。不得在 M2 以“补全查询”为由修改端口之外的结构，M2 结束后也不再允许新增未声明的算子。

对已有完整子树的插入，可以将其标为可替换端口，并在 M1 记录旧子树及接口。M2 只替换该局部区域，且不得丢弃端口外部引用的导出列。支持“空端口填充”还是“完整子树替换”须写入 `embedding_mode`；首版可只启用前者，不能将后者默认为已支持。

### 4.2 PatternSpec

每条规则至少声明以下内容：

```python
# 拟议协议；不是当前仓库已有 API。
class PatternSpec:
    identity: tuple[str, str, str]  # catalogue, pattern_id, form
    version: str
    family: str                    # symmetry / short_circuit
    source_interface: Interface
    replacement_interface: Interface
    local_source: AbstractFragment
    declared_alternative: AbstractFragment | LocalRewrite
    applicability: tuple[SemanticPremise, ...]
    state_requirements: tuple[ConstraintTemplate, ...]
    work_objective: WorkObjective
    affected_region: RegionSelector
    local_relation: RelationContract
    required_features: frozenset[str]
```

`state_requirements` 表达端点唯一性、逆边双射、真假/NULL 条件、逐绑定见证、候选规模等事实，统一降低为求解器需求。规则中允许存在具体数学前提；禁止以规则 ID 直接返回完整 `_build_p3` 式图配方。`work_objective` 描述希望变化的候选、扫描、路径扩展或重复输入规模，不写“必定慢于另一侧”。

### 4.3 GraphState 与 BindingPlan

| 记录 | 必需字段 |
|---|---|
| `GraphState` | `graph_spec`、`schema`、`indexes`、`role_domains`、`facts`、`checks`、`construction_certificate`、`graph_digest` |
| `BindingPlan` | `slot_assignments`、`symbol_domains`、`binding_relations`、`occurrence_bindings`、`source_and_alternative_coverage`、`plan_digest` |
| `ConstructionRecord` | `embedded_digest`、`target_id`、`constraint_digest`、`accepted_plan_digest`、`graph_digest`、`search_trace`、`constraint_provenance`、`remaining_obligations` |

图中实体域与查询终端赋值不是同一对象：例如 `s` 的实际绑定域为唯一 Start 节点，而标签槽位绑定为 `Start`，属性键槽位为 `id`，字面量槽位为整数 `1`。`BindingPlan` 同时覆盖宿主外层、嵌入结构和高成本备选的全部槽位；不能只填 Pattern 内部。

M3 可以保存尚未枚举的实体域，但必须选择并冻结一个满足全部约束的终端赋值方案。若输出的是有限联合方案集合，必须在 M3 中决定具体方案并记录选择；M4 不再独立抽取各槽位，以免破坏跨槽位约束。构造记录区分 `proved_in_model`、`requires_loaded_state_check` 和 `unknown`，数据库加载前不把待检查事实写成已验证。

### 4.4 查询对与依赖报告

`QueryPair` 至少保存 `query_a_ir`、`query_b_ir`、`target_id`、`mutation_direction`、`changed_region`、`origin_map`、`graph_digest`、`binding_plan_digest`、`relation_contract`。短路关系另外保存 `envelope_site` 与决定性事实；被替换分支自身不要求相等。

`DependencyReport` 由两个独立字段组成：

- `static`：依赖节点/边、每层传播摘要、前提、跨 Pattern 影响、未知原因、目标暴露状态。
- `runtime`：未运行/目标不支持/计划无法映射/已取得证据，数据库版本、计划、完整结果比较、重复测量和确认记录。

不得用一个 `passed=true` 同时表示模型等价、数据库等价和存在性能异常。

## 5. 六个接口与实现细节

以下签名均为拟议接口。M1–M6 是方法文档与实现验收共用的阶段标识。

### M1：先随机生成抽象 AST

```python
# 拟议公开接口；两个生成层都位于 M1 内。
generate_random_ast(grammar, capability_profile, budget, host_seed) -> HostAst
```

M1 采用两层生成。第一层依照通用子句语法与合法后继关系生成 `ClauseSkeleton`，例如 MATCH→WITH→RETURN，也允许没有前置 MATCH 的 RETURN 宿主。第二层依照位置要求的语法类别、期望类型和当前作用域，递归展开宿主的图匹配项、表达式与子查询结构。只有第二层完成后，产物才是供 M2 使用的 `HostAst`。

该分层借鉴 GDsmith 的骨架与类型驱动补全，但没有引入其具体图引导输入。M1 不读取 PatternSpec、GraphManager、GraphState、Vals 或 `varToProperties`；不能先读取某图，再从图中选择匹配路径。M1 可以决定“读取可见节点的一个整数属性”，将属性键保存为整数类型终端槽位；不能在这一阶段选择“恰好等于 7 的图属性”。后一要求由 M3 在固定结构上求解。

以下为拟议内部接口，不新增方法阶段：

```python
# 拟议接口，均由 generate_random_ast 内部调用。
sample_clause_skeleton(grammar, capability_profile, clause_budget, seed) \
    -> ClauseSkeleton
expand_host_structure(skeleton, grammar, capability_profile, budget, seed) \
    -> HostAst
expand_match_shape(request, scope_state, budget, rng) -> AbstractMatch
expand_expression_shape(request, scope_state, budget, rng) -> AbstractExpression
advance_scope(clause, incoming_scope) -> ScopeState
```

子句骨架中的普通语法槽位（例如 WHERE 条件、投影表达式、匹配项）必须在 M1 第二层展开。它们与保留给 M2 的 Pattern 端口不同：前者不能遗留给 M3 或 M4；后者具有完整的输入输出接口和独立标识。首版只保留一个未填 Pattern 端口；多端口模式的未选位置保留 M1 已生成的默认子树。所选端口类别与位置由通用类型语法决定，不以某个 Pattern 能否适配指导生成。

第二层按依赖顺序工作：先为 MATCH 展开匹配形状并登记变量，再生成可引用这些变量的 WHERE；先在输入作用域中生成 WITH 投影表达式与别名，再建立输出作用域并生成本系统支持片段中的 WITH WHERE。子查询递归调用相同产生式，相关导入通过 symbol ID 显式登记。`RETURN COUNT { ROW_HOLE }` 因而可以直接由返回整数的位置展开得到，不需要为它增加起始 MATCH。

```python
# 拟议 M1 控制伪代码；这里只采样结构，不求解图值。
def generate_random_ast(grammar, capabilities, budget, host_seed):
    skeleton = sample_clause_skeleton(
        grammar, capabilities, budget.clauses, derive_seed(host_seed, "clauses"))
    scope = empty_scope()
    tree, trace = [], []
    rng = seeded_rng(derive_seed(host_seed, "structure"))
    port_plan = select_typed_port(skeleton, grammar, budget, rng)
    for clause_slot in skeleton.clauses:
        clause = expand_clause_in_dependency_order(
            clause_slot, scope, port_plan, grammar, capabilities, budget, rng)
        scope = advance_scope(clause, scope)
        tree.append(clause)
        trace.append(record_structure_choices(clause, scope))
    host = freeze_host(tree, skeleton, trace, port_plan)
    require_only_graph_terminals_and_declared_ports_unfilled(host)
    validate_abstract_types_and_scopes(host)
    return host
```

`select_typed_port` 只预留类型化位置；若目标位置要等表达式展开后才能定位，则预留产生式路径并在展开时解析为稳定地址。`expand_clause_in_dependency_order` 与其内部递归调用共享预算账本，不能每进入一层子查询就重置预算。选择无参数叶子、类型化常量槽位或合法变量引用时同样记录产生式与作用域；无法收束时返回 `generation_exhausted`。

| 结构 | M1 的作用域与可空性操作 |
|---|---|
| 必选 MATCH | 读取输入绑定，登记新节点/关系变量；同名引用必须指向实际既有 symbol，不能隐式新建同名实体 |
| OPTIONAL MATCH | 区分既有输入与新导出；新匹配变量标记可能为 NULL，保留未匹配输入的接口；是否存在匹配留给 M3 |
| WITH | 投影表达式读取输入 scope；输出只包含显式保留变量和新别名；未投影变量对后续子句不可见。当前受支持 IR 的 WITH WHERE 在投影后作用域验证（`src/ir.py:698–730`） |
| 别名 | 保存表达式来源和推导类型；实体透传保留身份和可空性；同层兄弟别名不能未经语义支持相互引用 |
| COUNT/EXISTS 子查询 | 显式导入所需外层 symbol，局部定义不泄露；Count 返回非 NULL 整数，Exists 返回布尔值；空输入行为由 M3 推导 |
| CASE/coalesce/布尔式 | 依据操作符签名生成子类型及 nullable 标记；不把“可空”理解为实际值必定 NULL |
| UNION/条件行流 | 各分支在各自作用域展开，出口按列类型和可空性统一；仅生成目标能力与当前 IR 支持的形式 |

预算至少分为子句数、总 AST 节点数、表达式深度、子查询深度、匹配项/路径上界和端口预留节点预算。一次产生式扩展先预留完成各必要孩子的最低预算；不能靠超深度后填入非法默认节点收束。图节点数与边数不属于 M1 的可读取状态，而在 M3 的图预算中控制。

验收要求：固定语法、能力、预算和 `host_seed` 后，更换 Pattern 清单、Pattern 选择、GraphState、数据种子或 Vals 均不得改变 `ClauseSkeleton` 与 `HostAst` 的摘要；更直接的接口测试应确认这些对象根本没有传入 M1。Pattern 可影响 M2 的接受率，不能倒推改写归档的 M1 输出。

### M2：在兼容位置嵌入已定义 Pattern

```python
embed_patterns(host, registry, selection_policy, embed_seed, budget) -> EmbeddedAst
```

先枚举宿主端口，再筛选接口兼容的规则。用户指定某 Pattern 时可以在这里过滤规则；若无兼容端口，返回 `no_compatible_site`，由编排层重新生成宿主并记录失败。不得针对该 Pattern 重建一个外层 AST 后把它记为原宿主。

插入采用捕获规避重命名，绑定通过 symbol ID 接入。规则中需要的外部节点只能从端口可见绑定中选；无法绑定时可以在规则声明的局部区域定义新变量，不能在外层悄悄追加 MATCH。插入后验证端口输出接口与所有外部消费者一致，保存变异目标、合法替换区域、替代结构及来源映射。

首版每个查询对只测试一个目标实例；宿主允许包含其他登记实例。多个实例的重叠需要明确父子关系。一个插入导致另一个实例的规则位置消失、输出接口改变或前提冲突时，拒绝该组合或重新选择端口，不能只保留一个失效标记。

### M3：联合构造 Pattern-aware Graph State

```python
construct_graph_state(embedded, scale_spec, state_seed, solver_budget) \
    -> tuple[GraphState, BindingPlan, ConstructionRecord]
```

求解输入是完整嵌入 AST、目标规则及已声明的局部变异备选。构造方先根据备选声明形成内部候选树用于约束推导；这不是发布最终查询对。两侧共享不变实体的来源映射和全局图状态。

M3 借鉴 GDsmith GenExpr 的“父需求分解为子需求”，但分析对象是 M2 已固定的 AST。若当前节点是 AND，就只能分解这一个 AND；不能为满足 TRUE 而把它替换成 OR 或常量。结构不可满足时返回冲突或有限搜索失败，由编排层重新开始结构生成。目标值、基数区间及绑定域约束在本阶段共同求解，M1 不读取的图与值信息到这里才参与。

拟新增 `ConstraintTask(address, binding_domain_id, required_observation, quantifier, provenance)`，将需求绑定到实际词法位置和输入域。`quantifier=exists` 可用于“至少存在一个保留行”的目标；`quantifier=forall_reachable` 用于 S7 等规则在每个实际输入绑定上的决定性前提。两类义务不可混用：一个满足 WHERE 的见证可以支持非空目标，不能证明所有 u 的 `answer` 均非 NULL。

拟议内部接口为 `decompose_requirement(fixed_node, requirement, binding_domain) -> ConstraintAlternatives`。返回值仅包含对子节点的需求、域约束与冲突信息，不允许返回新的 AST。以下采用 T、F、N 表示 TRUE、FALSE、NULL，星号表示这三个语义值中的任一个；`unknown` 是分析器不知道，既不属于星号也不等于 N。

| 固定节点与父需求 | 合法的子需求分解 |
|---|---|
| `NOT e = T / F / N` | 分别要求 `e = F / T / N` |
| `a AND b = T` | 仅 `(T,T)` |
| `a AND b = F` | `(F,*)` 或 `(*,F)`，以备选约束分支表示 |
| `a AND b = N` | `(N,T)`、`(T,N)` 或 `(N,N)` |
| `a OR b = T` | `(T,*)` 或 `(*,T)`，与其他图事实联合选择可满足分支 |
| `a OR b = F` | 仅 `(F,F)` |
| `a OR b = N` | `(N,F)`、`(F,N)` 或 `(N,N)` |
| WHERE 保留一行 | 对该行绑定要求精确 T；F 与 N 均不保留行，但在后续其他观察中仍区分 |
| CASE 输出某值 | 条件为 T 时约束 THEN；条件为 F 或 N 时约束 ELSE；分支选择不改 AST |
| coalesce 输出由第 j 项决定 | 前 j−1 项为 N，第 j 项非 NULL 并满足目标值；后续项保持已有结构 |
| COUNT 条件 | 对其直接行流形成非负整数区间或区间并，再与共享需求求交；如 `count != 2` 不退化成单区间 |
| EXISTS 为 T/F | 对实际输入分别要求直接行流至少一行/零行；不跳过中间 RETURN 或聚合 |

空输入必须单独表示为绑定域基数 0。不能将“没有输入行”编码为某个表达式的 NULL，也不能利用全称命题的空域真值，把完全不可达的 S7 目标标成已获得有效决定性输入。需要非空输入的目标附加基数下界；S5/S9 所需的零需求或空输入则按各自合同保留。对空直接子查询，COUNT 的值为 0；若中间操作重新产生一行，应逐算子重新计算实际行域。

每个任务再保存 `analysis_status=proved/refuted/unknown`。未知来自未支持函数、无法封闭的共享输入域或预算耗尽时，不能据此选择一个 NULL 值并继续宣称前提已证明。构造可以回溯到其他候选赋值，但接受记录必须覆盖所有声明的实际输入域，或具有覆盖该域的构造证书。

约束分为四组，全部带来源位置：

1. 宿主约束：外层过滤、COUNT 区间、EXISTS 真假、WITH 列关系、分支输出及逐绑定可空性。
2. 规则前提：逆边配对、独立分支、决定性布尔值、非 NULL 前缀、见证行数等。
3. 数据与结构约束：节点/边身份、标签、属性类型、路径界、关系多重性、索引及跨区域共享/分离。
4. 工作量约束：候选与有效匹配的分离、图规模参数、被改变读取的候选规模，以及允许的外层调用域。

约束按实际 AST 与绑定传播，图构造器接收规范化需求而不是 Pattern ID。可以复用 `derive_requirement_plans` 与图构造组件，但需要消除“某 Pattern 只对应某固定区域”的入口依赖。反硬编码验收必须同时检查宿主生成独立性与构图接口：新增 Pattern 若能表达为已有可组合义务，应只增加规则定义，不应在通用构图流程加入按 Pattern ID 选择整图配方的分支。新增语义原语可以扩展通用求解器，但须以原语合同为边界。多条规则联合时，冲突必须指明涉及的 instance ID、槽位与 AST 地址。

枚举预算耗尽返回 `budget_exhausted`；仅当穷尽了声明的有限搜索域并有冲突证据时返回 `unsatisfiable_in_domain`。不能把有限构造器找不到解表述成任意图上无解。候选超出支持语法、路径模型或目标能力时返回 `unsupported_fragment`。

具体构造按下列有限搜索顺序实现，任一步失败均在预算内回到最近存在其他候选的选择点：

1. 从全部终端槽位的联合域枚举候选赋值，保留共享键、类型、区间和规则前提之间的约束；将源码树与声明备选解析成供求解使用的内部候选树，不发布查询。
2. 根据词法 symbol ID、导入关系和显式相同/分离约束建立实体域。使用等价类合并必须相同的域，并检查分离约束；不得按变量拼写合并不同作用域的节点。
3. 从外层消费者需求建立实际输入绑定和行流见证：为计数下界分配有效行，为 EXISTS 建立见证，为每个 OPTIONAL 输入确定匹配或 NULL 扩展；先交叉检查行数与值域约束。
4. 将这些需求降低为通用匹配、路径、属性、实体绑定与逆边义务，调用相应有限构造组件实现节点组和边组。共享区域合并后重算连接产生的多重性，不以局部满足替代全树满足。
5. 选择受目标变异影响的工作轴，扩张候选、有效匹配或输入重复域。扩张遵循规则关系：失败候选必须被排除，短路区域由决定性事实保护，对称两侧保持匹配对应和多重性。
6. 扫描源树及备选中的全部读取，检查扩张域是否产生额外可观察匹配，并逐消费者验证两侧结果关系。允许同一 N 下两侧结果共同增长；不要求不同 N 之间结果固定。例如 P10 的有效输出多重性可随规模增长，条件是每个规模下两侧一致。
7. 用小规模完整解释执行核验构造见证；对压缩图族额外验证规模扩展证书。证书无法覆盖的新读取或复杂共享结构返回未知/不支持，不从几个小规模点外推。冻结 Graph State、BindingPlan 及待加载状态检查。

约束降低的首版映射如下；“拟新增”项属于新的记录或校验接口，不代表要引入通用 SMT 求解器。

| 规范化义务 | 当前可复用表示/组件 | 需补充内容 |
|---|---|---|
| 匹配下界、精确行数、候选数 | `MatchDemand`；`requirements` 的数量分配 | 端口/实例来源、外层消费位置、原/变异侧标识 |
| 路径长度、节点局部 ALL 真假/NULL | `PathDemand`、`path_population` | 不同路径观察的允许关系与对应证书 |
| 标量精确值、非 NULL、三值前提 | `ValueDemand`、`derive_requirement_plans` | `ConstraintTask` 的绑定域、量词及全域覆盖证据，不能只选一个示例绑定 |
| 输入/输出实体是否为 NULL | `EntityBindingDemand` | OPTIONAL 按输入域的状态分区及每区多重性 |
| 正逆关系逐边对应 | `InversePair` | 节点/关系身份观察限制与重复边检查 |
| 原/变异节点域相同或分离 | `BindingRelation` | 对端口导入、共享变量及捕获规避来源的转换 |
| 全树终端联合绑定 | 拟新增 `TerminalBindingConstraint` | 标签/键/字面量的联合域、接受赋值和覆盖性 |
| 两侧局部观察关系 | 拟新增 `ObservationRelationConstraint` | scalar/bag/path-bijection/envelope 关系与前提来源 |
| 外层输入与工作轴 | 复用既有 work-axis 推导，拟新增 `InputDomainConstraint` | m 的真实绑定域、增长影响和未决物理关系 |

通过后冻结 Graph State 和 BindingPlan。所有数据库状态检查在计时前执行。有限构造组件只能在各自支持的片段内给出见证；不应把上述流程包装成任意 Cypher 的全能求解器。

### M4：根据已实现图状态实例化整个查询

```python
instantiate_query(embedded, graph_state, binding_plan) -> InstantiatedQuery
```

对完整树递归遍历，将每个终端槽位替换为 M3 接受的标签、类型、属性键、常量或参数。验证全部外层子树和全部局部备选都有绑定覆盖。声明过但未选为变异目标的 Pattern 也必须有完整绑定。

该操作是终端物化（materialization），不是 GDsmith QueryFiller 意义上的结构补全。拟议内部函数 `materialize_terminals(tree, accepted_assignments)` 只访问 `TerminalSlot`，不调用 M1 的任何展开器，也不运行 `GenPatterns`、`GenExpr` 或任何根据图重新选择表达式的过程。普通语法槽位若到 M4 仍未展开，应返回 `unexpanded_structure`；不能把它作为图绑定缺失处理后补造子树。

M4 不改变算子数量、类型、作用域边、投影列数、分支数或路径结构；路径上下界若属于终端槽位，其值必须来自 M3 已冻结的联合方案。缺少绑定时返回 `unbound_slot`；不能在此补默认属性、随机造图或新增长链。

输出包括基准查询 IR、已实例化备选载荷、`instantiation_record`、各槽位来源和结构摘要。实例化后的形状摘要应等于“忽略终端值”的 M2 摘要，Graph State 摘要和 BindingPlan 摘要保持不变。随后执行完整 IR 类型与作用域验证。

### M5：只执行目标 Pattern 的局部变异

```python
mutate_target(instantiated, graph_state, target_id) -> QueryPair
```

重新验证目标位置与 M2 登记一致，以及 M3 的规则前提仍有效。应用 Pattern 声明的变异，保存移动/保留/新增局部节点的来源映射。结构变异只能发生在声明区域；M4 已实例化的备选被整体放入时，新增节点必须来自 M2 的备选声明，不能临时生成替换查询。

对称变换使用局部值相等、多重集相等或路径双射关系。短路变异使用决定性上下文的关系：低潜在工作分支 L 被替换为高潜在工作分支 H，整个决定性外壳及完整查询保持语义一致；L 与 H 自身可返回不同值或行数。

归档方向统一明确写为 `symmetry`、`L_to_H` 或旧记录的 `H_to_R`，并额外记录每侧角色，不从文件名或 A/B 顺序推断。当前论文组合支路在 `src/symbolic.py:717` 记录 L→H，`src/generator.py:2091–2094` 将 source、target 分别放入 a、b；而 `src/executor.py:737` 将 S1–S10 视为参考比较，`:845` 要求 a 为较慢侧才进入候选确认。这形成了需要迁移时修正的方向合同不一致：L→H 用例中 b 变慢可能被旧门槛过滤。本轮只确认该源码条件，未作数据库复现。

执行器应按 `mutation_direction` 与每侧角色确定筛选方向；S 类标识不能代替方向记录。旧 SCor 高成本查询到参考替换的记录仍按 H→R 解码，不能在迁移时静默交换其 a/b。

### M6：检查依赖关系并收集运行时证据

```python
check_dependencies(pair, embedded, graph_state, evidence_context=None) \
    -> DependencyReport
```

首先静态检查；只有语义前提与结果传递关系已证明，才通过现有编译和执行适配器获得动态证据。静态语义被否定或未知时，直接返回对应状态及 `runtime=not_run`。无目标连接时返回静态报告与未运行状态。

静态分析必须沿 AST 祖先、实际定义—使用边和行流生产—消费边共同传播，不能只靠变量名称、树的几何包含关系或 Pattern 编号判断。每个传播步骤记录源位点、消费者、作用域、输入事实、输出摘要及使用的规则版本。

| 层次 | 需要计算的内容 | 结果类型 |
|---|---|---|
| 值依赖 | 子表达式的值、NULL、行多重集或路径观察能否影响消费者 | `value_dependency=used/unused/conditional/unknown`；另以 `result_relation=proved/refuted/unknown` 记录两侧结果关系，两个字段不能互相替代 |
| 逻辑需求 | 消费者需要全部行、一个见证、前 k 行，还是已被决定性条件屏蔽 | `full / witness / bounded / masked / unknown`，附逐绑定前提 |
| 调用多重性 | 相关输入域和重复输入行如何使局部逻辑求值被请求 | 精确值或上下界、依赖键；物理调用次数另记未知 |
| 跨 Pattern 影响 | 内层变异是否改变外层规则的前提、见证域、暴露或可观测目标 | 每个实例独立保存 `premise_status=satisfied/refuted/unknown` 与 `exposure_status=exposed/masked/conditional/unreachable/unknown` |

例如 COUNT 通常观察输入行数；EXISTS 只观察空/非空；LIMIT 需要输出顺序或同值投影合同；WITH 必须区分普通投影、过滤、DISTINCT 以及聚合；OPTIONAL 必须逐输入行判断 NULL 扩展。上述内容约束语义和逻辑需求，不意味着物理计划一定按语法顺序求值。

对于 m 行相关输入，逻辑请求数可能为 m；数据库可能去相关、缓存或物化。只有可映射到目标位置的执行计划/算子证据才能支持物理调用数量。计划没有相应计数时明确记录 `physical_invocations=unknown`，不能从 m 直接填入实际次数。

语义前提不成立或语义传播为 unknown 时，该用例不能作为已经通过等价性检查的正式性能用例；实际结果恰好相等也不替代缺失的规则依据。逻辑需求、调用规模或物理映射为 unknown，而语义关系已经成立时，可以保留用例并记录其未决维度。

运行时依次执行：加载并检查 G；完整执行两查询并比较有类型结果多重集；取得目标数据库版本与可用计划；按现有测量配置进行预热、成对随机顺序测量和独立确认。图状态检查失败、查询出错、结果不完整或结果不等时，不进入普通性能比较；超时保留独立分类及已有参考验证证据。计划证据与非分析模式计时分开保存。完整查询差异成立但局部算子无法映射时，可以记录完整查询性能候选，局部性能传导仍为 `unknown`。

ValScope 的借鉴边界是“定位修改点 → 沿实际使用依赖逐层传播摘要 → 到达根部”。原论文 §4.4、Algorithm 1（论文页码 394–395）传播的是语义近似关系，不是本系统的性能摘要。来源：[ValScope 原论文](https://www.usenix.org/system/files/osdi26-lin-li.pdf)。本节四层性能检查是待实现的 GraphSymPerf 设计，不能归为 ValScope 已证明的性能定理。

## 6. Pattern 目录、变异与图需求

当前论文目录由 `src/paper_patterns.py:22–45` 定义：P1–P13 和 S1–S10。旧目录由 `src/mutator.py:16–43` 定义：P1–P16 和 S1–S10。新记录保留 `catalogue=paper-v1` 与 `method_version=ast-pattern-v1` 两个独立字段；方法版本不是 Pattern 编号版本。

以下“图需求”是新 PatternSpec 必须声明的最低合同与可选工作量设计，不是声称当前每条规则已经具备通用求解能力。具体规模分配由联合求解器完成，不能据表生成固定完整图模板。

| 论文 ID / form | 旧 ID | 局部变换 | 最低图/语义需求与工作量设计 |
|---|---|---|---|
| P1 `edge_mirror` | P1 | 无向单边的端点书写顺序交换 | 保留原变量及全部约束；逐绑定匹配双射；可构造两端候选域差异 |
| P2 `inverse_edge` | P2 | 正向关系类型与配对逆向关系切换 | 逐边逆向双射、端点与多重性一致；不得暴露不保持的关系身份/属性；可构造类型或端点选择性差异 |
| P3 `bounded_path` | P3 | 有界无向路径反向书写 | 有界路径双射，消费者反转不变；有效路径与失败候选分离；可控制失败位置和候选增长 |
| P4 `and` | P4 | AND 操作数顺序交换 | 纯、确定、全定义的三值布尔语义；可构造选择性不同且读取规模不同的条件 |
| P5 `or` | P5 | OR 操作数顺序交换 | 同上；包括 TRUE/FALSE/NULL 的准确传播；不以语法顺序保证物理短路 |
| P6 `equality` | P6 | 等式两侧交换 | 保持类型与 NULL 语义；当前规则不泛化成任意比较算子交换；可设置属性候选分布 |
| P7 `labels` | P7 | 合取节点标签顺序交换 | 相同标签集合和交集；可配置标签基数差异及索引状态 |
| P8 `types` | P8 | 关系类型备选顺序交换 | 相同类型集合、关系多重性；可配置类型分布差异 |
| P9 `predicate_placement` | P9 | 在合法必选 MATCH 间移动谓词 | 前后位置均具备同一词法输入；不越过 OPTIONAL/作用域边界；可配置早筛选的候选差异 |
| P10 `independent` | P10 | 独立必选 MATCH 顺序交换 | 各分支无跨引用、输出多重集乘积一致；可配置分支基数不均衡 |
| P10 `shared_anchor` | P11 | 同锚点必选分支顺序交换 | 每个实际锚点绑定上的条件多重集乘积一致；可配置逐锚点分支基数 |
| P10 `chain` | P12 | 合法链式必选 MATCH 顺序交换 | 共享连接变量身份不变、联合多重集相同；谓词所需变量已绑定；可配置前缀工作差异 |
| P11 `optional_branches` | P13 | 独立 OPTIONAL 分支顺序交换 | 必选锚点；无分支间引用；逐锚点匹配/不匹配及 NULL 扩展一致；覆盖四种分支存在组合 |
| P12 `union_all` | P14 | UNION ALL 分支顺序交换 | 列接口一致、多重集并一致；不能被无证明的无序 LIMIT 观察先后；可配置分支规模差异 |
| P13 `case_dual` | P15 | 空值安全地取反条件并交换 THEN/ELSE | 使用 `NOT coalesce(c,false)` 处理 NULL；分支类型相容；可配置分支读取规模 |
| S1 `absorbing_or` | S1 | 将决定性 TRUE 的 OR 中被保护项 L 换为 H | 每个实际输入绑定的决定项为 TRUE；L/H 同布尔接口，可有不同局部结果 |
| S2 `absorbing_and` | S2 | 将决定性 FALSE 的 AND 中被保护项 L 换为 H | 决定项精确为 FALSE，不能用 NULL 代替；高成本区域与决定项事实相容 |
| S3 `conditional` | S3 | 将非选中查询分支 L 换为 H | 原生条件查询能力；决定条件及分支输出 schema 一致；非选中分支的行可不同 |
| S4 `case_guard` | S4 | 将 CASE 非选中标量分支 L 换为 H | 决定条件及选中分支固定；备选类型相容；FALSE/NULL 的 ELSE 行为分别核验 |
| S5 `empty_limit` | S5 | LIMIT 0 内的 L 换为 H | 全外壳输出仍为空；H 的读取域可增长；不能将“空结果”误判为无意义测试 |
| S6 `bounded_demand` | S6 | 需求已满足时替换后续行流 L→H | 每个输入绑定的保留见证数达到 k；当前保守合同使用同值投影和流式分支；保留 LIMIT envelope |
| S7 `coalesce` | S7 | 已有非 NULL 前缀时替换后续参数 L→H | 前缀中更早参数为 NULL，决定参数非 NULL；0、false、空字符串均非 NULL；L/H 类型相容 |
| S8 `existential_witness` | S8 | 已有见证时替换其他候选区域 L→H | 每个实际输入绑定有保留见证；EXISTS 仍为 TRUE；候选备选可改变局部多重集 |
| S9 `empty_input` | S9 | 空必选输入后的独立读取 L→H | 精确空输入和合法下游接口；不跨会重新产生行的聚合；不把 OPTIONAL 空分支当作零输入 |
| S10 `membership` | S10 | 空集合语义外壳中的成员读取 L→H | 保持决定性空输入、NULL 安全成员谓词与 DISTINCT 外壳；当前表达不等于任意原生集合算子 |

旧 P16 是量词输入组顺序，不在当前论文目录内。迁移时保留历史身份，不将它无说明地塞入论文 P13 或任意 S 模式。

## 7. 两个完整实施样例

### 7.1 P3：随机 COUNT 宿主中的路径镜像

M1 可能随机生成如下宿主。`ROW_HOLE` 是行流端口，外层 COUNT 是已经确定的结构，不因选择 P3 而追加。

```text
Return(
  Alias(Count(ROW_HOLE), "n")
)
```

M2 将 P3 声明的路径查询插入端口。此时标签、属性键和常量以带角色的终端槽位存在。M3 接受以下绑定与图；M4 后的示意查询为：

```cypher
RETURN COUNT {
  MATCH p=(s:Start {id: 1})-[:T*2..2]-(t:Target)
  WHERE all(x IN nodes(p) WHERE x.keep = true)
} AS n
```

给定整数 N≥1，联合构造器产生 `s,a,t,b,z1,…,zN`，共 N+4 个节点。仅有 T 边 `s-a`、`a-t`、`s-b`、`b-z_i`，共 N+3 条边。`s` 是唯一 `Start(id=1)`；`t,z_i` 均有 Target 标签；除 `b.keep=false` 外，所有节点 `keep=true`。每条边均有独立身份，不额外引入平行边。

采用该规则支持的两跳路径语义，存在 N+1 个符合端点与长度约束的候选路径：`s-a-t` 和 N 条 `s-b-z_i`。ALL 仅接受 `s-a-t`，因此 COUNT 为 1。M3 要保存候选数、通过数及属性事实的检查；这组具体图是说明约束如何落地的样例，不是要求每次 P3 都调用此固定图配方。

M5 仅将路径项改为：

```cypher
RETURN COUNT {
  MATCH p=(t:Target)-[:T*2..2]-(s:Start {id: 1})
  WHERE all(x IN nodes(p) WHERE x.keep = true)
} AS n
```

`s` 的 Start 标签和 id 约束、ALL 谓词、变量身份和外层 COUNT 均保持不变。路径反转建立双射；ALL 为节点局部不变量，COUNT 仍为 1。M6 记录 COUNT 的行数需求、路径失败候选及目标计划证据，不由 N+1 个候选推断数据库必然枚举全部路径，也不保证两侧耗时不同。

若 M1 原本生成了 m 个 Root 的逐行输入及 `Root-[:LINK]->s` 相关绑定，则额外记录输入多重性；m 必须来自真实输入行域，不能事后为了放大 P3 在宿主外层添加该结构。相同 s 的重复绑定可能被数据库缓存或去相关，物理重复执行仍需证据。

### 7.2 S7：非 NULL 前缀下的低工作分支到高工作分支

M1 已确定外层查询及标量端口，M2 在端口嵌入 S7。M3 构造 m>0 个 Root，每个 Root 有不同的整数 id；对每个实际输入绑定 u 约束 `u.answer=0`，并构造一个 REF 邻居和 N>1 个 HIGH 邻居。两个关系域不通过额外可见读取破坏宿主事实。M4/M5 得到两个完整查询：

```cypher
// Q_L
MATCH (u:Root)
RETURN u.id AS id,
       coalesce(u.answer, COUNT { MATCH (u)-[:REF]->(v) }) AS value
```

```cypher
// Q_H
MATCH (u:Root)
RETURN u.id AS id,
       coalesce(u.answer, COUNT { MATCH (u)-[:HIGH]->(v) }) AS value
```

两式对每个实际 u 均为整数 0；0 是非 NULL 值。被替换子查询的 COUNT 分别为 1 和 N，不要求二者相等。变异方向归档为 `L_to_H`，等价范围从 coalesce 外壳开始向外传播。目标是检查后续高工作分支是否仍导致额外代价；其语义被屏蔽正是 S7 测试的组成部分。

若 u 的输入域包含 `answer=NULL` 的绑定，必须重新求解或拒绝这一决定性前提，不能只检查一个示例 u。若宿主其他子查询也能读到新增 HIGH 边，应联合检查这些读取；不能凭“关系名字不同”假定整个查询不受影响。

### 7.3 嵌套 Pattern 的两种状态

当 P3 位于 S7 的 fallback 中，M6 分别记录：S7 的决定性前提成立，其短路测试目标有效；P3 的对称关系可能仍成立，但作为独立性能目标被外层非 NULL 前缀屏蔽。保存 S7 的 `premise_status=satisfied`、`target_valid=true`，以及 P3 的 `result_relation=proved`（证明成立时）、`exposure_status=masked`，而不是删除整个用例。

若要研究 P3 的暴露情形，应在另一份完整归档用例中求解允许 fallback 被请求的外层状态，再重新验证所有 Pattern 前提。不得在计时中临时将 `u.answer` 改成 NULL，并仍沿用原用例的 Graph State 摘要或 S7 前提。

## 8. 编排、失败与预算

下面的伪代码表示控制边界；其中编译、检查与测量属于 M6 的证据获取，不增加第七个方法阶段。

```python
# 拟议控制流。
def generate_case(config):
    for attempt in range(config.generation_budget):
        host = generate_random_ast(
            config.grammar, config.capabilities,
            config.ast_budget, derive_seed(config.seed, "host", attempt))
        record_stage("M1", host)

        embedded = embed_patterns(
            host, registry, config.selection_policy,
            derive_seed(config.seed, "embedding", attempt), config.embedding_budget)
        record_stage("M2", embedded)

        state, plan, construction = construct_graph_state(
            embedded, config.scale_spec,
            derive_seed(config.seed, "state", attempt), config.solver_budget)
        record_stage("M3", state, plan, construction)

        source = instantiate_query(embedded, state, plan)
        record_stage("M4", source)

        pair = mutate_target(source, state, embedded.target_id)
        record_stage("M5", pair)

        report = check_dependencies(pair, embedded, state, config.evidence_context)
        record_stage("M6", report)
        if report.static.result_relation != "proved":
            record_failure("M6", "semantic_relation_unproved", report)
            continue
        if report.runtime.result_relation == "refuted":
            record_failure("M6", "result_mismatch", report)
            continue
        return AcceptedOrClassifiedCase(host, embedded, state, plan, pair, report)

    return Failure(stage="pipeline", code="generation_budget_exhausted")
```

实际实现应让每个接口返回结构化 `Result`，由编排层决定重试范围；上面为简化省略解包。`AcceptedOrClassifiedCase` 明确区分仅静态有效、已取得执行证据、性能候选和未决性能状态；语义被否定或未证明均不返回正式接受的性能用例。不能用一个大范围 `except ValueError: continue` 丢弃阶段、原因和不支持信息。

| 失败代码 | 含义 | 允许动作 |
|---|---|---|
| `generation_exhausted` | M1 预算内无法完成合法宿主 | 换宿主种子；计入拒绝分母 |
| `no_compatible_site` | 无可容纳所选规则的端口 | 换端口/规则或重新生成宿主；保留原失败记录 |
| `binding_capture` / `interface_mismatch` | 嵌入破坏作用域或输出接口 | M2 拒绝；修正规则或选择其他端口 |
| `constraint_conflict` | 具体约束分支冲突 | 在 M3 预算内回溯，保存冲突来源 |
| `unsatisfiable_in_domain` | 有限声明域已穷尽且无解 | 返回有范围的无解结论 |
| `budget_exhausted` | 未完成穷尽搜索 | 返回未知；不能称无解 |
| `unsupported_fragment` / `unsupported_target` | 构造器或目标缺能力 | 单列统计，不静默转 legacy |
| `unbound_slot` / `unexpanded_structure` / `contract_drift` | M4 缺终端绑定、仍有普通结构槽位或输入被改动 | 作为实现错误暴露，禁止自动补默认值或调用结构生成器 |
| `premise_failed` / `out_of_region_mutation` | M5 前提或变异范围不成立 | 拒绝并保留反例；不进入计时 |
| `result_mismatch` | 目标数据库上完整结果不一致 | 语义失败；不能作为性能异常 |
| `semantic_relation_unknown` | 语义前提、输入域覆盖或等价传递依据不足 | 不进入正式性能比较；单列未知及缺失依据 |
| `work_dependency_unknown` | 语义已证明，但逻辑需求或工作传播缺依据 | 可计时，未知工作关系不能据此作局部归因 |
| `plan_mapping_unknown` | 语义已证明，但物理计划无法可靠映射目标位点 | 可保留完整查询测量；局部物理传导保持未知 |

预算分别限制 AST 节点/深度、嵌入尝试、联合约束分支、图节点/边、执行时长和确认次数；不得用单一规模 N 同时代指所有预算。多 Pattern 组合还需实例数和重叠深度上限。所有随机选择使用派生子种子归档，避免额外重试改变后续无关阶段的随机轨迹。

## 9. 版本与回放

新运行记录新增 `method_version=ast-pattern-v1`、`grammar_version`、`pattern_catalogue_version`、每条规则版本、语义/构造器版本和目标能力版本。本次文档修订号 `gdsmith-r1` 只记录设计修订，不自动改变方法版本或表示运行结果。M1 另存子句骨架与结构展开的派生种子、摘要和轨迹。保留源代码版本及工作区状态摘要，不能仅凭随机种子要求跨版本逐字再生。

每个完整用例至少归档：

```text
manifest.json
M1-clause-skeleton.json
M1-expansion.json
M1-host.json
M2-embedding.json
M3-state.json
M3-bindings.json
M3-construction.json
M4-instantiation.json
M5-pair.json
M6-dependencies.json
M6-runtime.json
queries/                  # 已编译查询与参数，作为可检查输出
```

这是拟议文件布局；也可在现有归档中存为同名分段，但必须保留全部证据与摘要。每阶段记录输入摘要、输出摘要、阶段版本和失败状态；不得通过删除可选字段绕过合同检查。

回放提供两个明确模式：

1. **执行回放**：直接加载已归档 IR、参数、Graph State 和检查，重新编译或核对目标后执行；不重新随机生成。
2. **生成再现**：在完全匹配的版本和种子下重新执行 M1–M5，比对逐阶段摘要；不匹配时报告首次差异，不能覆盖原记录。

旧 `legacy-v1` 及旧 symbolic 三阶段记录继续使用对应解码器。它们不能被标成 `ast-pattern-v1`；缺少 M1 宿主和 M2 插入证据的旧记录无法证明新方法顺序。`paper-v1` 的 P10 三种 form 和旧编号映射显式保留。

## 10. 方法—接口—测试—证据追踪

下表中“新增测试”是待实现验收合同；现有测试只提供可复用断言，没有在本轮执行。

| 方法 ID | 拟议接口 | 必须验收的性质 | 归档证据 | 可复用现有测试 |
|---|---|---|---|---|
| M1 | `generate_random_ast` | 子句骨架与有类型展开均不依赖 Pattern/具体图/值表；类型、作用域、可空性和预算合法 | `M1-clause-skeleton`、`M1-expansion`、`M1-host`、shape digest | `tests/test_composition.py:25,47` 的接口/捕获断言；两层生成与独立性测试待新增 |
| M2 | `embed_patterns` | 只修改声明端口；捕获规避；完整记录原树与插入位置；多个同形位置只选目标 | `M2-embedding`、outside-port digest、origin map | `tests/test_dependencies.py:36,84,102`；`tests/test_mutation_origins.py:31,61` |
| M3 | `construct_graph_state` | 固定 AST 上分解需求；两侧共图；真实绑定域与量词明确；三值、数量、空输入及 unknown 不混同 | 图、绑定、ConstraintTask 来源/域/量词、构造/扩展证书 | `tests/test_requirement_domains.py:70,118,132,244,269`；`tests/test_truth_contracts.py:41,115`；新域覆盖合同待测试 |
| M4 | `instantiate_query` | 整树终端物化，零未绑定终端；无普通结构槽位；不调用生成器、不改图 | `M4-instantiation`、槽位覆盖、各摘要 | `tests/test_symbolic_skeleton.py:50,61,91`；需新增外层覆盖与禁止结构补全测试 |
| M5 | `mutate_target` | 指定单实例局部变异；完整结果关系与 NULL/多重性/路径前提成立 | `M5-pair`、变异方向、关系和范围 | `tests/test_dependencies.py:483,504,540,751`；`tests/test_paper_rejections.py:27,42,56,78,109` |
| M6 | `check_dependencies` | 四层摘要分离；屏蔽与短路目标有效性分开；物理未知不冒充逻辑事实 | `M6-dependencies`、版本/计划/运行记录 | `tests/test_dependencies.py:161,178,577` 仅可复用语义传播；性能传播与计划证据测试需新增 |

编号兼容参考 `tests/test_paper_catalog.py:9–17,31–39`；旧阶段记录防篡改参考 `tests/test_symbolic_pipeline.py:159–181`。`tests/test_all_paper_patterns.py:62,69` 检查的是当前 symbolic 入口，不能在新验收中更名后继续声称证明 M1/M2。

### 10.1 必须覆盖的端到端用例

| 验收用例 | 预期性质，不是实测结果 |
|---|---|
| 同一宿主、多个兼容 Pattern | M1 归档一致；M2 选择不同局部规则；外部结构保持 |
| 同一 Pattern、不同宿主和深度 | 不调用完整 Pattern 模板；覆盖行流、谓词、标量等允许接口 |
| 多个同形候选位置 | 按 instance ID 只改变一个目标；其余位置和绑定保持 |
| P3 的 N 参数族 | 模型上 N+4 节点、N+3 边、N+1 候选、1 个通过；两查询 COUNT=1；目标计时另行记录 |
| P3 的顺序敏感路径消费 | 返回路径节点序列等不满足反转不变的观察时拒绝，不能仅看外层 COUNT |
| S7 的 answer=0 与 answer=NULL | 0 接受为非 NULL 决定值；NULL 情形不能沿用同一前提 |
| S6/S8 逐绑定见证不足 | 某输入绑定缺见证时拒绝，不能用总见证数代替每绑定事实 |
| P11 的四种匹配组合 | both/left-only/right-only/neither 逐输入域保持 NULL 扩展与行多重性 |
| 冲突外层 COUNT 区间 | 返回有来源的约束冲突/预算未知，不改阈值、不额外加成功行 |
| 两个 Pattern 共享实体域 | 检查一个实例增加的图域对所有读取的影响；冲突不能被命名隔离掩盖 |
| S7 内部嵌套 P3 | S7 测试有效；P3 暴露为 masked；结果不压成全局 invalid |
| m 行相关输入 | 逻辑输入多重性可核验；无计划证据时物理调用次数保持 unknown |
| 目标优化器消除两侧差异 | 记录无差异或目标擦除变换；不将生成成功当作漏洞 |
| 损坏 Graph State/绑定/位点 | 各阶段摘要与前提校验拒绝继续执行 |
| 旧编号与旧归档回放 | 旧 P11/P13 等身份保持；不会转换成错误论文 Pattern |
| 短路比较方向 | L→H 的 b 变慢与 H→R 的 a 变慢均按各自角色进入筛选；不能统一要求 a 变慢 |

### 10.2 报告指标

生成阶段分别报告宿主生成数、结构摘要种类数、端口种类/深度分布、嵌入接受率、联合构造接受率和各失败原因。结构多样性统计忽略变量重命名与终端值变化，并分别统计宿主结构和 Pattern 局部结构；不能把改随机变量名当作结构覆盖增长。

正确性报告槽位覆盖率、局部变异范围违规数、构造检查失败数、目标数据库完整结果不一致数和回放一致率。性能依赖报告静态已解析/未知数量、实例暴露/屏蔽分布、计划可映射比例和具有运行时支持的依赖数量。每个比例注明分母和失败样本的计入方式。

性能测试复用现有成对测量与确认阈值，并归档实际配置。报告运行时间比、绝对差、方向一致性及确认结果；DB Hits、行数和算子循环数用作机制证据。本文不填写尚未测量的数值，也不将静态预测的工作增长写成运行时间复杂度结论。

### 10.3 GDsmith 借鉴部分的新增合同测试

以下均为待实现测试，不是本次已执行的测试结果。

| 测试 ID / 阶段 | 输入或扰动 | 验收断言 |
|---|---|---|
| GD1 / M1 | 固定语法、能力、预算和 host seed，改变 Pattern 清单、图状态、数据 seed、Vals | 两层摘要不变；M1 接口与依赖中不出现这些运行时输入 |
| GD2 / M1 | 覆盖 MATCH 开头与直接 RETURN 的合法子句骨架 | 两者均可由通用语法产生；`RETURN COUNT { ROW_HOLE }` 不被强制补上 MATCH |
| GD3 / M1 | WITH 丢弃节点变量、保留变量、实体透传别名 | 丢弃变量不能被后继引用；透传保持词法身份与可空性；重名不等于同一绑定 |
| GD4 / M1 | OPTIONAL 导出的 nullable 实体、嵌套相关子查询 | nullable 信息传递，局部定义不泄露；输入绑定通过显式 imports 接入 |
| GD5 / M1 | 普通 WHERE/投影结构槽位、唯一 Pattern 端口、深度预算边界 | 普通槽位在 M1 内展开完；只保留声明端口和图终端；递归总预算不重置 |
| GD6 / M3 | NOT 的 T/F/N 目标，以及 AND/OR 的全部九种子值组合 | 约束分解与独立三值真值表一致；NOT 的 T 目标要求子值 F，NULL 保持 NULL |
| GD7 / M3 | 同一 AST 的外层目标分别要求 T、F、NULL 或矛盾 COUNT 区间 | AST 结构摘要保持；允许不同图/β 或有来源的冲突；不得以改算子/阈值逃避冲突 |
| GD8 / M3 | 未知值、已知 NULL、空绑定域、空 COUNT 输入 | 四者分类保持；unknown 不产生成功证书，空 COUNT 为 0，空域不是一行 NULL |
| GD9 / M3 | 两个实际 u 中只有一个 answer 非 NULL | 一个满足见证不能证明 S7 的全域前提；应构造覆盖全部绑定的事实或拒绝 |
| GD10 / M4 | 正常终端赋值、残留普通结构槽位，监测所有结构生成器调用 | 正常物化不增长 AST；残留结构返回 `unexpanded_structure`；不调用 QueryFiller 式结构补全 |

生成统计补充子句骨架种类数、展开后宿主结构种类数、结构展开拒绝率与作用域拒绝原因；它们均不能以变量名/终端变化计数。M3 报告“见证满足”与“实际输入域覆盖已证明”两类数量，避免将单见证非空率当作规则全域有效率。

## 11. 实施顺序与切换条件

1. 固定数据合同与新版本，建立 M1/M2 的独立性、作用域和插入范围测试。先让一个兼容 Pattern 完整贯通，但宿主生成器从第一天起不接收该 Pattern 的 ID。
2. 将现有局部规则整理为 PatternSpec；提取 `GraphSpec` v2、需求求解与解释器适配。用 P3 与 S7 分别贯通对称和短路流程，建立原树—嵌入—构图—填充—变异的逐阶段证据。
3. 拆分 `instantiate_pair`，补齐外层所有槽位的覆盖测试；将已核查语义边界逐条迁入注册表。按表推进 P1–P13/S1–S10，不以登记条目数量等同于贯通数量。
4. 实现 M6 四层静态摘要及嵌套 Pattern 分类，再接 Neo4j 的完整结果、计划与计时证据。先保留 unknown，再逐步扩展可证明范围。
5. 新入口通过各阶段合同及端到端验收后，改 `prepare_case` 的新生成选择；旧入口只接受显式 legacy/replay 模式。禁止新入口失败后回退旧 Pattern 模板。
6. 逐目标扩展后端验证。完成当前版本的归档回放与统计后，方法文档的实现状态才可更新为已实现；数据库性能结论以实际运行证据为准。

最终切换条件不是“文件已按六步命名”，而是任一用例都能从 M1 原始宿主、M2 指定插入、M3 联合状态、M4 全树终端赋值、M5 单点变异追踪到 M6 依赖与执行证据，且每一步的实现不绕过其输入输出合同。

## 12. 文档验证与修订范围

### 12.1 初稿的具体语义例子验证

初稿阶段对配套方法稿的三段、技术稿的四段完整 Cypher 使用当时工作区的 `query.parse_query` 与 `ir.validate_query` 进行了只读检查，均可解析并通过 IR 校验；两文共享的 P3 查询对与 S7 源查询经统一渲染后完全一致。进一步使用当时的 `population.evaluate_query` 和显式 GraphSpec v2 检查以下说明例：

| 样例 | 初稿阶段观察 |
|---|---|
| P3，N=1、3、20 | 两侧均返回 `[{"n": 1}]`；独立邻接枚举核对候选为 N+1，节点为 N+4，边为 N+3 |
| S7，(m,N,answer)=(1,3,0)、(3,5,0) | 两侧均返回 m 行整数 0 |
| S7，m=0 | 两侧均为空多重集；与存在实际决定性输入的样例分开 |
| S7，m=1、N=3、answer=NULL | 两侧分别得到 1 和 3，形成缺失非 NULL 前提的反例 |
| 嵌套 COUNT 与 EXISTS | `RETURN EXISTS { RETURN COUNT { MATCH (a:Missing) } AS n } AS e` 在没有 Missing 节点的显式小图上返回 `[{"e": true}]`，验证需求必须经过中间 RETURN |

这些检查支持文档中的具体语义例子，并未运行新的 M1–M6 管线，也未连接真实数据库或产生性能测量结果；不能将其解释为本次新增接口通过测试。

### 12.2 `gdsmith-r1` 的定向修订

本次重新核对 GDsmith 正式论文及第 2.3 节固定提交的相关源码，更新 M1 两层生成、M3 固定 AST 的需求分解、M4 终端物化边界，以及对应字段、归档和测试合同。M2/M5/M6 的规则与 P3/S7 示例保持原文；六阶段顺序和 `ast-pattern-v1` 方法版本保持。

本次仅更新两份设计文档，没有实施新接口、改变源码或执行第 10.3 节的规划测试。新增设计仍为待实现；初稿语义例子验证与本次文档修订分别记录，既有测试套件未在本次整体重跑。

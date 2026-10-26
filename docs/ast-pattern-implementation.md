# GraphSymPerf：随机 AST 与 Pattern 嵌入的代码技术设计

配套的完整中文方法稿见 [六阶段方法汇报稿](GraphSymPerf-六阶段方法汇报稿.md)。本文统一称“中间结果表”（简称“结果表”）；工程类型 `RowStream` 表示产生这类结果的查询结构，不预设物理执行必须逐行进行。

方法版本：`ast-pattern-v1`；文档修订：`implementation-r3`。日期：2026-10-26。配套方法文档：[随机 AST 与 Pattern 嵌入的方法](ast-pattern-method.md)。

本文说明当前 `ast-pattern-v1` 实现及其验收合同。六个阶段统一标识为 M1–M6：随机生成 AST、嵌入模式（Pattern）、构造模式感知图状态（Pattern-aware Graph State）、实例化完整查询、执行局部变异、检查依赖关系。`Src/ast_pattern/` 已实现六阶段模块，`main.prepare_case()` 和常规运行配置默认使用新入口。本文将实际接口、压缩图上的证据范围及尚未接入的扩展要求分别注明；冻结版本、测试结果和数据库验收记录见 [实现验收记录](ast-pattern-implementation-status.md)。

## 1. 实现目标与不可混同的对象

新入口先生成与具体 Pattern 无关的抽象语法树（Abstract Syntax Tree，AST），再检查预留接口并嵌入兼容 Pattern。原有“选择 Pattern → 构造内核 → 向外包装”保留为显式 `symbolic-v1` 兼容路线；有限模板保留为显式 `legacy-v1` 路线。新入口复用已有类型系统、局部规则、需求求解、显式图表示及执行器，不在失败后回退兼容路线。

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

## 2. 重构前基线与当前入口

### 2.1 重构前入口与路线差异

本小节行号对应 Git 检查点 `9898a69`，用于说明迁移来源；当前入口见第2.4节。

以下表格保存实施前的差距审计，行号对应当时的源码，不用于定位当前入口。

| 重构前代码位置 | 当时已观察行为 | 新路线中的处理 |
|---|---|---|
| `Src/main.py:311`，`prepare_case` | 调用 `instantiate`；缺少 `instantiation_record` 时执行 `mutate`；最后保存 IR 并编译 | 改为 M1–M6 显式编排；不根据某字段是否存在隐式选择路线 |
| `Src/generator.py:1990`，`instantiate` | 接受 Pattern；`:2026` 调用 `abstract_seed`；`:2037` 调用 `_BUILDERS[pattern]` | 保留旧记录回放能力；退出新生成入口 |
| `Src/generator.py:2070`，`_instantiate_composed` | 顺次调用 `build_symbolic_skeleton`、`construct_state`、`instantiate_pair` | 不能直接更名为新六阶段实现 |
| `Src/symbolic.py:263`，`build_symbolic_skeleton` | 按 Pattern 构造局部 kernel；`:386` 才调用 `compose_fragment` 包装外层 | 拆出可复用局部规则；不保留为新宿主生成器 |
| `Src/composition.py:96`，`compose_fragment` | 从已有 kernel 向外添加 `exists/count/with` | 提取算子的输入输出语义；不以该包装顺序实现 M1 |
| `Src/symbolic.py:444`，`construct_state` | 先绑定候选语法，再依据全树与变异备选构造状态 | 复用求解组件，改为 M3 的联合状态与绑定合同 |
| `Src/symbolic.py:572`，`instantiate_pair` | 校验构造记录，实例化 source；`:639` 已调用 `mutate` | 拆成 M4 与 M5，分别保存记录 |
| `Src/dependencies.py:1361`，`propagate_equivalence` | 沿实际祖先传播路径双射、标量相等和结果多重集相等 | 保留为语义证明组件，不能直接充当性能传播检查 |
| `Src/main.py:270`，`compile_case`；`Src/executor.py:729`，`run_case` | 目标编译、状态和结果校验、计时及确认流程 | 通过明确适配层接入 M6 的运行时部分 |

组合支路对外部种子还在 `Src/generator.py:2085` 记录 `provenance_only_not_embedded`，所以不能把现状写成“在任意输入查询 AST 中嵌入 Pattern”。

### 2.2 从基线复用的能力与目标边界

| 能力 | 源码证据 | 复用方式与边界 |
|---|---|---|
| 有类型 IR 与作用域 | `Src/ir.py:23–213`；`row_stream_imports:467`、`row_stream_schema:478`、`validate_query:694` | 复用节点类型和校验器；抽象槽位校验需要补充 |
| 结果表和标量语法 | `Src/row_grammar.py:22`；`Src/scalar_grammar.py:24` | 提取通用产生式；移除宿主生成对特定 law 的依赖 |
| 结构位置与词法绑定 | `Src/dependencies.py:21`、`:36`、`:106`、`:191`、`:207` | 复用定位、替换、定义—使用与行消费记录 |
| 显式变异标记 | `Src/symbolic.py`，`MutationMark` | 当前关联一个登记实例与实际位点；多个显式实例仍为扩展 |
| 显式图表示 | `Src/state.py:87`，`GraphSpec`；`:114–125`、`:150–152` | 复用 v2 图组；v1 Pattern 配方仅用于旧回放 |
| 图需求与求解 | `Src/population.py:29–110`、`:367`；`Src/requirements.py:1570` | 复用 Match/Path/Value/Binding 等需求，加入端口、规则实例和终端槽位来源 |
| 局部变异与节点来源 | `Src/mutator.py:641`、`:646`、`:661`、`:744` | 将规则派发收敛到 Pattern 注册表，保留语义守卫 |
| 小图解释器 | `Src/population.py:411`，`evaluate_query` | 用于构造检查和反例；实际目标语义仍须数据库核验 |
| 全图增长观察闭合 | `Src/relational_population.py:915–937` | 复用增长图域与读取位置的关系；其结果不是执行时工作量证明 |
| 结果与计时 | `Src/executor.py:538`、`:577`、`:583`、`:729` | 复用完整有类型结果多重集比较、AB/BA 调度和独立确认 |

当前 `main.compile_case()` 与执行器继续对 GraphSpec v2 的非 Neo4j 加载返回不支持。新路线的真实图加载验收以 Neo4j 为目标；其他目标仍须分别完成加载、语义和证据适配。回放可以保留目标不支持或编译失败状态，但这不表示对应后端已完成新路线验收。

### 2.3 GDsmith 的采用机制与落点

本次借鉴依据为 Ziyue Hua、Wei Lin、Luyao Ren、Zongyang Li、Lu Zhang、Wenpin Jiao、Tao Xie，2023，*GDsmith: Detecting Bugs in Cypher Graph Database Engines*，ISSTA 2023，pp. 163–174，[DOI](https://doi.org/10.1145/3597926.3598046)。[正式论文 §3.2.1–3.2.3、Table 1、Algorithms 2–3](https://taoxiease.github.io/publications/issta23-gdsmith.pdf) 将子句骨架、图引导的匹配结构和数据引导的表达式生成分开组织。本文选择采用子句骨架、有类型递归展开、变量生命周期与需求分解机制；GDsmith 先有图再生成匹配与条件的次序不移入 GraphSymPerf 的 M1。

源码核查固定在公开提交 [`cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217`](https://github.com/ddaa2000/GDsmith/commit/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217)。下表源码路径均位于该提交的 `Src/main/java/org/example/gdsmith/cypher/` 下；它们是设计来源，不是本仓库已引入的依赖。

| GDsmith 来源 | 已核查机制 | GraphSymPerf 采用位置与调整 |
|---|---|---|
| [`gen/query/ModelBasedQueryGenerator.java:18–33,54–68`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/query/ModelBasedQueryGenerator.java#L18) | 递归选择合法后继子句；先形成序列，再调用 QueryFiller | M1 内先生成通用子句骨架，再展开宿主结构；源码中固定初始 MATCH 不作为本系统要求，允许既有 `RETURN COUNT { … }` 宿主 |
| [`dsl/QueryFiller.java:37–74`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/dsl/QueryFiller.java#L37) | MATCH 先补匹配后补条件，WITH 先补别名后补条件 | M1 的结构展开按依赖顺序进行；原 QueryFiller 会生成结构，不能作为 M4 的终端实例化实现 |
| [`standard_ast/Symtab.java:163–195,244`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/standard_ast/Symtab.java#L163)；[`With.java:19–20`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/standard_ast/With.java#L19) | 子句符号表按匹配定义与投影导出组织变量可见性 | M1 维护显式输入/输出作用域、别名来源和可空性；依照本系统支持的 IR 规则处理 WITH，不复制名称列表充当绑定身份 |
| [`gen/expr/NonEmptyExpressionGenerator.java:263–264,291–342,358–425`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/expr/NonEmptyExpressionGenerator.java#L263) | 以期望类型筛选变量；条件携带 BooleanAssertion；递归构造表达式 | 类型驱动的结构展开进入 M1；期望值分解进入 M3。M3 只分析已经固定的算子，不调用该生成器重新选择结构；按三值真值表独立实现与验证，包括 NOT 的目标值取反 |
| [`gen/query/GraphGuidedQueryGenerator.java:18–44`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/query/GraphGuidedQueryGenerator.java#L18) | 持有 GraphManager，匹配/条件/别名生成共享 `varToProperties` | M1 禁止读取具体图与该类值表；M3 建立基于实际绑定域的联合值约束，不能以一个映射值替代所有输入的事实 |
| [`gen/pattern/SlidingPatternGenerator.java:47–90,194–198`](https://github.com/ddaa2000/GDsmith/blob/cce9c4bc8bda155a6c39bbd3ef2ceebf2c9ee217/src/main/java/org/example/gdsmith/cypher/gen/pattern/SlidingPatternGenerator.java#L47) | 从已有图区域取得子图、转为匹配项并记录属性信息 | 借鉴实体映射与共享变量的显式记录；不在 M1 从图抽取结构。这里的 graph pattern 是 Cypher 图匹配项，不是 P3/S7 等预定义测试 Pattern |

GDsmith 的 GenExpr 同时选择结构并满足值约束。本系统将两项职责分到 M1 与 M3，分别冻结结构和求解图/终端绑定。M4 只物化已接受终端；GDsmith 的“完成骨架”不能直接对应本系统的“实例化查询”。

### 2.4 当前实现入口索引

| 阶段或职责 | 当前源码与入口 | 已实现行为 |
|---|---|---|
| M1 | `Src/ast_pattern/host_generator.py::generate_random_ast` | 子句骨架与有类型展开，返回一个显式端口及符号终端 |
| M2 | `Src/ast_pattern/embedding.py::embed_patterns`；`pattern_registry.py` | 23 个论文规则、P10 形式选择、局部结构固定、捕获规避及端口外结构校验 |
| M3 | `Src/ast_pattern/state_builder.py::construct_graph_state` | 有限联合终端赋值、既有需求与图求解、GraphSpec v2、绑定与证书冻结 |
| M4 | `Src/ast_pattern/instantiation.py::instantiate_query` | 校验构造合同，实例化源查询及声明备选，不调用变异器 |
| M5 | `Src/ast_pattern/mutation.py::mutate_target` | 再校验 M4 检查点，应用指定单点变异，保留验证查询和关系证据 |
| M6 | `Src/ast_pattern/dependency_check.py::check_dependencies` | 根部结果关系、实际逻辑输入、需求、外层已存在 Pattern 与共享读取分析 |
| 运行证据 | `Src/ast_pattern/evidence_adapter.py`；`Src/executor.py` | 执行前验证阶段与编译合同，按 side roles 筛选，将运行报告绑定到图和查询摘要 |
| 编排与再现 | `Src/ast_pattern/pipeline.py` | 有限重试、失败轨迹、M1–M6 摘要链、源码指纹及 `reproduce_generation` |
| 公共入口与归档 | `Src/main.py::prepare_case/run/save_case/read_case` | 默认新方法，兼容方法显式选择，格式 4 阶段文件与输入校验 |

`PatternSpec.build_local()` 调用从原 `symbolic.py` 提取的 `build_local_pattern()`，仅产生局部规则结构。新入口不调用旧 `build_symbolic_skeleton()`，M3/M4/M5 则使用明确的语义适配与 `prepare_instantiation()` / `finish_mutation()` 两个检查点。原有 `instantiate_pair()` 仅保留组合这两个检查点的兼容行为。

## 3. 当前模块划分与依赖方向

下列模块已位于 `Src/ast_pattern/`，使用现有 IR 表示查询。模块职责对应六阶段，动态执行与静态分析通过证据适配器连接。

```text
Src/ast_pattern/
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

`pyproject.toml` 已将 `ast_pattern` 包与原有模块一起纳入安装。通用构图器仍消费实际 AST 推导的需求，规则专有代码集中于局部语法与语义合同；阶段测试检查新入口不会调用旧完整骨架或有限模板生成器。

## 4. 数据合同

### 4.1 宿主、端口与嵌入记录

以下为 `contracts.py` 中的实际记录字段；摘要属性由字段计算，并不重复保存为构造参数。

| 记录 | 实际字段或属性 | 合同 |
|---|---|---|
| `ClauseSkeleton` | `clauses`、`transition_trace`、`skeleton_seed`；属性 `skeleton_digest` | 子句项含 `clause_id/kind/syntax_slots`；M1 内部产物 |
| `ScopeState` | `scope_id`、`parent_scope`、`visible_symbols`、`definitions`、`exports`、`alias_sources`、`nullable_types` | 可见性、词法身份、别名来源与可空性分开；无实体取值 |
| `HostAst` | `tree`、`ports`、`slots`、`scope_graph`、`clause_skeleton`、`expansion_trace`、`host_seed`、`grammar_version`、`budget`；属性 `shape_digest/host_digest` | 普通结构展开完毕，只保留图终端和一个显式端口 |
| `TerminalSlot` | `slot_id`、`kind`、`candidates`、`type`、`owner_scope`、`shared_with` | 有限终端域；不能以终端候选携带 AST 结构 |
| `Port` | `port_id`、`kind`、`expected_type`、`imports` | 插入前树中的实际占位节点 |
| `EmbeddingPort` | `port_id`、`address`、`kind`、`visible_symbols`、`input_schema`、`output_schema`、`observation_contract`、`reserved_budget`、`default_subtree` | 地址须指向对应 Port；端口预算限制局部插入 |
| `PatternInstance` | `instance_id`、`pattern_id`、`form`、`port_id`、`site_address`、`imports`、`exports`、`rule_version` | 当前登记一个目标实例 |
| `EmbeddedAst` | `host`、`tree`、`skeleton`、`instances`、`target_id`、`origin_map`、`outside_port_digest`；属性 `embedded_digest` | `skeleton` 是现有语义求解器的适配对象，不重新生成外层结构 |

当前 M1 产生一个显式未填端口，M2 填入一个登记的 Pattern。局部结果表可按端口接口原样插入，或经 `COUNT`、`EXISTS` 转成整数、布尔表达式；这些接口提升由 M2 声明并记录。M2 检查将插入区域换回原 Port 后整棵宿主的摘要不变。

多端口默认子树、任意已有子树替换和同时登记多个嵌套 Pattern 仍是扩展合同。`default_subtree` 及 `instances` 的容器字段为此保留，但不能据字段存在宣称通用多实例嵌入或联合求解已经完成。

### 4.2 PatternSpec

`pattern_registry.py::PatternSpec` 当前保存：

```python
# 当前数据字段，省略 dataclass 装饰及方法体。
pattern_id: str
form: str
legacy_pattern: str
family: str
applicability: tuple
state_requirements: tuple
work_objective: str
local_relation: str
version: str
catalogue: str
interfaces: tuple
affected_region: str
```

`identity`、`source_interface`、`replacement_interface`、`local_source`、`declared_alternative`、`required_features`、`native_scalar` 为属性；`build_local(options, seed)` 才生成局部抽象结构和备选载荷。`local_source` 与 `declared_alternative` 的序列化值是产生式/变换描述，不是已经构造的完整宿主或图。

`state_requirements` 与适用前提提供可检查的规则声明，局部 AST 中的 intents/goals 再降低为共享求解器需求。规则不返回完整 GraphSpec 配方。`work_objective` 表示希望改变的候选或被屏蔽工作，物理耗时方向由执行证据确认。

### 4.3 GraphState、BindingPlan 与构造记录

| 对象 | 当前字段和证据存放位置 |
|---|---|
| `GraphState` | `graph_spec`、`population`、`embedded_digest`、`graph_digest`、`solved`、`facts`；属性 `spec` 返回 GraphSpec |
| `BindingPlan` | `slot_assignments`、`embedded_digest`、`graph_digest`、`plan_digest` |
| M3 构造记录 | 字典，继承语义求解器的 `skeleton_digest/assignment_digest/population_digest/site` 等字段，并加入 `method_stage`、`embedded_digest`、`graph_digest_v1`、`binding_plan_digest`、终端尝试次数及固定 AST 需求分解轨迹 |
| 图事实与验证 | `population.constraints/evidence/checks/expected_rows`，以及 `facts` 中的构造记录、`certificate_digest/facts_digest` |
| schema 与索引 | GraphSpec 的节点/关系图组、属性及 `index_requirements`；不另存一份可漂移的 schema |

图实体域与终端赋值分别由求解器证据和 `slot_assignments` 表示。M3 在有限候选域中选择并冻结一个联合赋值，M4 不再独立抽样。GraphState 本身不作为完整可回放对象直接序列化；归档显式保存图、BindingPlan、构造记录及 population 证据。

兼容求解器沿用的 `graph_digest` 与新合同中的规范化摘要编码不同；M3 记录用 `graph_digest_v1` 关联新 `GraphState.graph_digest`。阶段间必须比较对应字段，不能把两个编码下的字符串当作同一摘要。

### 4.4 实例化、查询对与依赖报告

`InstantiatedQuery` 保存 `source/replacement/bindings/checks/record/prepared/embedded/state/plan`。`prepared` 是源查询检查点，M4 未执行目标变异。

M5 复用 `symbolic.QueryPair`：`query_a/query_b/bindings/mutation_record/instantiation_record/checks`。目标 ID、变异方向、图摘要、联合绑定摘要、嵌入摘要及两侧查询摘要位于 `mutation_record` 和 `bindings` 中。短路替换的完整 envelope、决定性事实及加载检查保留在实际 site 和验证记录中。

`DependencyReport(static, runtime)` 的静态部分包含语义传播、输入域、需求和交互证据。运行时部分初始为 `not_run`，执行器将独立运行报告绑定到同一图、查询和目标；物理计划到 AST 位点的映射、局部物理调用次数当前保留 `unknown`。一个成功状态不能同时表示模型等价、数据库等价和性能异常。

## 5. 六个接口与实现细节

以下六个公开边界均已有实现。文中单独标注的算法伪代码用于说明顺序，不代替真实函数签名。M1–M6 是方法、阶段记录与测试共用的标识。

### M1：先随机生成抽象 AST

```python
# 当前公开接口；两个生成层都位于 M1 内。
generate_random_ast(grammar, capability_profile, budget, host_seed) -> HostAst
```

M1 采用两层生成。第一层依照通用子句语法与合法后继关系生成 `ClauseSkeleton`，例如 MATCH→WITH→RETURN，也允许没有前置 MATCH 的 RETURN 宿主。第二层依照位置要求的语法类别、期望类型和当前作用域，递归展开宿主的图匹配项、表达式与子查询结构。只有第二层完成后，产物才是供 M2 使用的 `HostAst`。

`typed-host-v2` 的搜索空间按以下维度组合，而非枚举完整查询模板：

| 维度 | 当前候选及组合方式 |
|---|---|
| 子句骨架 | `MATCH`、`OPTIONAL MATCH`、`WITH`、`WITH DISTINCT`、`RETURN`、`RETURN DISTINCT`；返回形式只出现在末尾 |
| 匹配形状 | `node`、`edge`、`chain`、`branch`、`converge`、`cycle`、`disconnected`；后五种由多个 MATCH 共享或分离变量构成 |
| 匹配约束 | 节点多标签、关系类型备选、节点/关系属性映射、命名关系变量；单边方向可为正向、反向或无向 |
| 投影 | 中间投影的变量保留与丢弃、标量别名、多列返回、投影/返回去重 |
| 标量结构 | 比较、空值判断、AND/OR/NOT、CASE、coalesce、COUNT、EXISTS、类型相容的叶子；布尔连接及 coalesce 可有多个操作数 |
| 子查询结果表 | 普通查询、UNION ALL、条件结果表；不含目标端口的固定分支还可包含行数截断 |

多 MATCH 的链与闭环不等价于把各边写进同一 MATCH 的多段路径：关系唯一性规则按实际子句边界解释。普通宿主不生成单边自环，也不生成额外裸有界路径。现有路径构图器要求单个有界路径、命名路径和节点局部 ALL 观察；该能力继续用于 P3 局部规则，不能据此宣称普通宿主支持任意数量的变长路径。形状示意见 [外层搜索空间说明](ast-host-search-space.md)。

该分层借鉴 GDsmith 的骨架与类型驱动补全，但没有引入其具体图引导输入。M1 不读取 PatternSpec、GraphManager、GraphState、Vals 或 `varToProperties`；不能先读取某图，再从图中选择匹配路径。M1 可以决定“读取可见节点的一个整数属性”，将属性键保存为整数类型终端槽位；不能在这一阶段选择“恰好等于 7 的图属性”。后一要求由 M3 在固定结构上求解。

M1 的当前内部入口为：

```python
sample_clause_skeleton(grammar=None, capability_profile=None, clause_budget=None, seed=0)
expand_host_structure(skeleton, grammar=None, capability_profile=None, budget=None, seed=0)
advance_scope(clause, incoming_scope)
validate_abstract_types_and_scopes(host)
```

匹配、表达式和子结果表的递归展开分别由内部 `_Builder.matches()`、`_Builder.expression()`、`_Builder.rows()` 实现，类型与可空性由 `_symbol_type()` 及 `advance_scope()` 推导。匹配组返回若干独立的 `ir.Match`，每条均推进作用域，并计入所在查询块的子句预算。原设计中的 `ExpansionRequest`、`expand_match_shape()`、`expand_expression_shape()` 并未作为独立 API 引入；其参数职责由 builder 的期望类型、scope、深度及预算状态承担。

子句骨架中的普通语法槽位（例如 WHERE 条件、投影表达式、匹配项）必须在 M1 第二层展开。它们与保留给 M2 的 Pattern 端口不同：前者不能遗留给 M3 或 M4；后者具有完整的输入输出接口和独立标识。当前只保留一个未填 Pattern 端口，位置在最终 RETURN 表达式的子树中；顶层 MATCH/WITH 的 WHERE 与 WITH 投影不直接承载该显式端口，返回表达式内的嵌套查询过滤条件可以承载。多端口、任意已有子树替换与默认子树仍为扩展要求。所选端口类别与位置由通用类型语法决定，不以某个 Pattern 能否适配指导生成。

第二层按依赖顺序工作：先为 MATCH 展开匹配形状并登记变量，再生成可引用这些变量的 WHERE；先在输入作用域中生成 WITH 投影表达式与别名，再建立输出作用域并生成本系统支持片段中的 WITH WHERE。子查询递归调用相同产生式，相关导入通过 symbol ID 显式登记。`RETURN COUNT { ROW_HOLE }` 因而可以直接由返回整数的位置展开得到，不需要为它增加起始 MATCH。

UNION ALL 与条件结果表在分支间统一列名、列顺序和类型；UNION ALL 只向一个分支传递 `need_port=True`，条件结果表则在守卫及两个分支中选择一个位置，保证全树恰好一个显式端口。普通结果表查询还可将该路径置于匹配后的 WHERE。行数截断只在 `need_port=False` 的子树中生成。当前等价传播不接受包含目标的无序 LIMIT，因此不能把目标放进该结构再依赖 M6 偶然接受。

```python
# 与当前公开 M1 一致的调用顺序，省略参数校验。
limits = _budget(budget)
skeleton = sample_clause_skeleton(
    grammar, capability_profile, limits["clauses"], derive_seed(host_seed, "clauses"))
host = expand_host_structure(
    skeleton, grammar, capability_profile, limits, derive_seed(host_seed, "structure"))
return replace(host, host_seed=host_seed)
```

`expand_host_structure()` 遍历子句骨架并共享 `_Builder` 预算；返回表达式递归生成时使用 `need_port=True`，只在类型与剩余深度允许的位置放入 Port。结束时要求恰好一个实际 Port，并建立对应地址与可见符号的描述，再验证抽象类型与作用域。两层的派生种子和选择轨迹随 HostAst 一并保存。

| 结构 | M1 的作用域与可空性操作 |
|---|---|
| 必选 MATCH | 读取输入绑定，登记新节点/关系变量；同名引用必须指向实际既有 symbol，不能隐式新建同名实体 |
| OPTIONAL MATCH | 区分既有输入与新导出；新匹配变量标记可能为 NULL，保留未匹配输入的接口；是否存在匹配留给 M3 |
| WITH | 投影表达式读取输入 scope；输出只包含显式保留变量和新别名；未投影变量对后续子句不可见。当前受支持 IR 的 WITH WHERE 在投影后作用域验证（`Src/ir.py:698–730`） |
| 别名 | 保存表达式来源和推导类型；实体透传保留身份和可空性；同层兄弟别名不能未经语义支持相互引用 |
| COUNT/EXISTS 子查询 | 显式导入所需外层 symbol，局部定义不泄露；Count 返回非 NULL 整数，Exists 返回布尔值；空输入行为由 M3 推导 |
| CASE/coalesce/布尔式 | 依据操作符签名生成子类型及 nullable 标记；不把“可空”理解为实际值必定 NULL |
| UNION/条件结果表 | 各分支在各自作用域展开，出口按列类型和可空性统一；仅生成目标能力与当前 IR 支持的形式 |

宿主配置中的 `grammar` 可限制 `clauses/clause_kinds/port_kinds/return_type/expression_operators`，以及新增的 `match_shapes/match_features/row_operators/comparison_operators`。`capabilities` 对同名候选取交集，`distinct=false` 禁用去重形式；能力限制作用于嵌套查询，不能只过滤顶层骨架。当前有序比较仅接受 IR 可以确认的数值类型，属性类型仍未知时不直接生成属性的 `<`、`<=`、`>`、`>=` 比较；等值比较和空值判断仍可观察属性。

| M1 预算 | 默认值 | 约束对象 |
|---|---:|---|
| `clauses` | 4 | 每个查询块展开后的子句数量，包含匹配组产生的各条 MATCH |
| `nodes` | 180 | 整棵宿主 AST 的共享节点总量 |
| `expression_depth` | 3 | 标量结构递归深度 |
| `subquery_depth` | 3 | COUNT/EXISTS 子查询嵌套 |
| `match_edges` | 3 | 一组匹配形状中的边数量 |
| `projections` | 2 | 生成投影的宽度上界 |
| `expression_arity` | 3 | 多元 AND/OR/coalesce 的操作数数量上界 |
| `path_hops` | 3 | 保留的路径预算字段，不使普通宿主额外获得裸有界路径能力 |
| `port_nodes` | 128 | M2 可在预留端口中放入的局部 AST 节点数 |

各递归分支共享 AST 节点账本；深度与子句限制按相应语法边界计量。预算不足时只能使用合法的终结形式或返回结构化生成失败。图节点数与边数不属于 M1 的可读取状态，而在 M3 的图预算中控制。候选数量增大不意味着每个组合都可构图：M3 仍联合检查全树与 Pattern 前提，M6 仍检查实际消费者和目标有效性。

验收要求：固定语法、能力、预算和 `host_seed` 后，更换 Pattern 清单、Pattern 选择、GraphState、数据种子或 Vals 均不得改变 `ClauseSkeleton` 与 `HostAst` 的摘要；更直接的接口测试应确认这些对象根本没有传入 M1。Pattern 可影响 M2 的接受率，不能倒推改写归档的 M1 输出。

### M2：在兼容位置嵌入已定义 Pattern

```python
embed_patterns(host, registry, selection_policy, embed_seed, budget) -> EmbeddedAst
```

先枚举宿主端口，再筛选接口兼容的规则。用户指定某 Pattern 时可以在这里过滤规则；若无兼容端口，返回 `no_compatible_site`，由编排层重新生成宿主并记录失败。不得针对该 Pattern 重建一个外层 AST 后把它记为原宿主。

插入采用捕获规避重命名，绑定通过 symbol ID 接入。规则中需要的外部节点只能从端口可见绑定中选；无法绑定时可以在规则声明的局部区域定义新变量，不能在外层悄悄追加 MATCH。插入后验证端口输出接口与所有外部消费者一致，保存变异目标、合法替换区域、替代结构及来源映射。

当前 `embed_patterns()` 要求一个声明端口且树中恰好一个实际 Port，返回一个 `PatternInstance`。按结果表观察的规则可经 Count/Exists 提升接入整数或布尔位置。P4/P5/P13/S1/S2/S4/S7/S8 等标量规则直接插入相容的标量或谓词端口，避免把其值投影包入计数后丢弃；S8 的原生结果为 EXISTS 的布尔值。首版原生标量形式将自由 guard、prefix、payload 表示为规则声明的符号字面量，图工作子查询在局部声明自己的锚点；imports 为空，即使宿主存在节点也不会擅自捕获。节点导入型原生 guard 属于后续扩展。宿主中原本存在的 coalesce、布尔吸收或 CASE 等上下文由 M6 识别，不计作第二个显式嵌入实例。多实例重叠、联合前提和身份保持仍需扩展专门的嵌入与求解合同。

### M3：联合构造 Pattern-aware Graph State

```python
construct_graph_state(embedded, scale_spec, state_seed, solver_budget) \
    -> tuple[GraphState, BindingPlan, ConstructionRecord]
```

求解输入是完整嵌入 AST、目标规则及已声明的局部变异备选。构造方先根据备选声明形成内部候选树用于约束推导；这不是发布最终查询对。两侧共享不变实体的来源映射和全局图状态。

M3 借鉴 GDsmith GenExpr 的“父需求分解为子需求”，但分析对象是 M2 已固定的 AST。若当前节点是 AND，就只能分解这一个 AND；不能为满足 TRUE 而把它替换成 OR 或常量。结构不可满足时返回冲突或有限搜索失败，由编排层重新开始结构生成。目标值、基数区间及绑定域约束在本阶段共同求解，M1 不读取的图与值信息到这里才参与。

已实现 `ConstraintTask(address, binding_domain_id, required_observation, quantifier, provenance)`，将需求绑定到实际词法位置和输入域。`quantifier=exists` 可用于“至少存在一个保留行”的目标；`quantifier=forall_reachable` 用于 S7 等规则在每个实际输入绑定上的决定性前提。两类义务不可混用：一个满足 WHERE 的见证可以支持非空目标，不能证明所有 u 的 `answer` 均非 NULL。

当前内部接口为 `decompose_requirement(fixed_node, requirement, binding_domain=None) -> ConstraintAlternatives`。返回值仅包含对子节点的需求、域约束与冲突信息，不允许返回新的 AST。以下采用 T、F、N 表示 TRUE、FALSE、NULL，星号表示这三个语义值中的任一个；`unknown` 是分析器不知道，既不属于星号也不等于 N。

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
| COUNT 条件 | 对其直接结果表形成非负整数区间或区间并，再与共享需求求交；如 `count != 2` 不退化成单区间 |
| EXISTS 为 T/F | 对实际输入分别要求直接结果表至少一行/零行；不跳过中间 RETURN 或聚合 |

空输入必须单独表示为绑定域基数 0。不能将“没有输入行”编码为某个表达式的 NULL，也不能利用全称命题的空域真值，把完全不可达的 S7 目标标成已获得有效决定性输入。需要非空输入的目标附加基数下界；S5/S9 所需的零需求或空输入则按各自合同保留。对空直接子查询，COUNT 的值为 0；若中间操作重新产生一行，应逐算子重新计算实际行域。

`ConstraintAlternatives` 保存 `alternatives/analysis_status/reason`，状态包括 `decomposed/proved/refuted/unknown`；局部分解只是后续义务，不代表图见证已经成立。当前 M3 对显式 value goals 作这一步预检并保存轨迹，再调用共享 requirements/population 求解器封闭完整输入域。局部规则未知时不能直接生成成功证书；只有后续共享求解器独立建立要求，整体构造才可接受。超过局部分解分支预算或不支持的函数保持未知；布尔显式枚举当前上限为5个项。

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
3. 从外层消费者需求建立实际输入绑定和结果表见证：为计数下界分配有效行，为 EXISTS 建立见证，为每个 OPTIONAL 输入确定匹配或 NULL 扩展；先交叉检查行数与值域约束。
4. 将这些需求降低为通用匹配、路径、属性、实体绑定与逆边义务，调用相应有限构造组件实现节点组和边组。共享区域合并后重算连接产生的多重性，不以局部满足替代全树满足。
5. 选择受目标变异影响的工作轴，扩张候选、有效匹配或输入重复域。扩张遵循规则关系：失败候选必须被排除，短路区域由决定性事实保护，对称两侧保持匹配对应和多重性。
6. 扫描源树及备选中的全部读取，检查扩张域是否产生额外可观察匹配，并逐消费者验证两侧结果关系。允许同一 N 下两侧结果共同增长；不要求不同 N 之间结果固定。例如 P10 的有效输出多重性可随规模增长，条件是每个规模下两侧一致。
7. 用小规模完整解释执行核验构造见证；对压缩图族额外验证规模扩展证书。证书无法覆盖的新读取或复杂共享结构返回未知/不支持，不从几个小规模点外推。冻结 Graph State、BindingPlan 及待加载状态检查。

当前约束适配如下。原设计中的部分概念由已有需求记录及证据字段承担，没有额外建立同名类。

| 规范化义务 | 当前可复用表示/组件 | 需补充内容 |
|---|---|---|
| 匹配下界、精确行数、候选数 | `MatchDemand`；`requirements` 的数量分配 | 端口/实例来源、外层消费位置、原/变异侧标识 |
| 路径长度、节点局部 ALL 真假/NULL | `PathDemand`、`path_population` | 不同路径观察的允许关系与对应证书 |
| 标量精确值、非 NULL、三值前提 | `ValueDemand`、`derive_requirement_plans` | `ConstraintTask` 的绑定域、量词及全域覆盖证据，不能只选一个示例绑定 |
| 输入/输出实体是否为 NULL | `EntityBindingDemand` | OPTIONAL 按输入域的状态分区及每区多重性 |
| 正逆关系逐边对应 | `InversePair` | 节点/关系身份观察限制与重复边检查 |
| 原/变异节点域相同或分离 | `BindingRelation` | 对端口导入、共享变量及捕获规避来源的转换 |
| 全树终端联合绑定 | `TerminalSlot` → `symbolic.Symbol` 适配及 `BindingPlan.slot_assignments` | 有限联合赋值、全部终端覆盖及摘要核验；无独立 `TerminalBindingConstraint` 类 |
| 两侧局部观察关系 | site guards、M5 `mutation_record`、既有 `propagate_equivalence` | scalar/bag/path-bijection/envelope 关系与前提来源；无独立同名约束类 |
| 外层输入与工作轴 | 既有 work-axis 推导、population evidence、M6 输入报告 | 真实逻辑绑定域、增长影响和未知物理关系；无独立 `InputDomainConstraint` 类 |

通过后冻结 Graph State 和 BindingPlan。所有数据库状态检查在计时前执行。有限构造组件只能在各自支持的片段内给出见证；不应把上述流程包装成任意 Cypher 的全能求解器。

EXISTS 包含 UNION ALL 时，增长闭包可以使用保留分支的稳定见证。`relational_population._stable_exists_union_witness()` 要求该分支具有实际逐输入的正数量义务，读取与所有增长 family 独立或受到局部排除，并在相关词法输入上确实产生行；它同时保留匹配数量检查用于数据库加载验证。缺少见证、仅覆盖部分输入、或增长区域影响该分支时，不会生成此证书。该规则按运算语义工作，不依据 S8 编号豁免图读取。

### M4：根据已实现图状态实例化整个查询

```python
instantiate_query(embedded, graph_state, binding_plan) -> InstantiatedQuery
```

对完整树递归遍历，将每个终端槽位替换为 M3 接受的标签、类型、属性键或字面量。全部宿主子树和声明备选均使用同一接受赋值。当前 IR 路线不生成外部 Parameter 节点，`parameters` 固定为空映射；编译器为某一目标产生的 per-side bindings 另行纳入执行合同。

该操作是终端物化（materialization）。当前通过 `validate_state_and_plan()`、`symbolic.prepare_instantiation()` 和 `_resolve()` 落实已接受赋值；未另建 `materialize_terminals()` API。M3 的 `fixed_skeleton()` 已拒绝残留 Port、结构 Hole 或携带 AST 的终端候选。M4 不调用 M1 的展开器或任何根据图重新选择表达式的生成器。

M4 不改变算子数量、类型、作用域边、投影列数、分支数或路径结构；路径上下界若属于终端槽位，其值必须来自 M3 已冻结的联合方案。缺少绑定时返回 `unbound_slot`；不能在此补默认属性、随机造图或新增长链。

输出包括基准查询 IR、已实例化备选载荷、`instantiation_record`、各槽位来源和结构摘要。实例化后的形状摘要应等于“忽略终端值”的 M2 摘要，Graph State 摘要和 BindingPlan 摘要保持不变。随后执行完整 IR 类型与作用域验证。

### M5：只执行目标 Pattern 的局部变异

```python
mutate_target(instantiated, graph_state, target_id) -> QueryPair
```

重新验证目标位置与 M2 登记一致，以及 M3 的规则前提仍有效，再应用 Pattern 声明的变异。局部来源由 M2 的 `origin_map` 保留；共享需求路径的原/变异对应关系位于 M3 的 requirements 证据，M5 不另建一份完整逐节点来源表。结构变异只能发生在声明区域；备选来自 M2 的局部声明和 M4 已接受终端，不能临时生成替换查询。

对称变换使用局部值相等、多重集相等或路径双射关系。短路变异使用决定性上下文的关系：低潜在工作分支 L 被替换为高潜在工作分支 H，整个决定性外壳及完整查询保持语义一致；L 与 H 自身可返回不同值或行数。

归档方向统一明确写为 `symmetry`、`L_to_H` 或旧记录的 `H_to_R`，并额外记录每侧角色，不从文件名或 A/B 顺序推断。新支路将源查询放入 a、变异查询放入 b，并归档 `mutation_direction/side_roles/reference_side`。原先按 S 标识要求 a 为较慢侧的判断已改为根据每侧角色确定参考方向；对应测试覆盖 L→H 的 b 变慢和历史 H→R 的 a 变慢。

执行器按 `mutation_direction` 与每侧角色确定筛选方向，S 类标识不再代替新用例的方向记录。旧 SCor 高成本查询到参考替换的记录仍按 H→R 解码，不能在迁移时静默交换其 a/b。

### M6：检查依赖关系并收集运行时证据

```python
check_dependencies(pair, embedded, graph_state, evidence_context=None) \
    -> DependencyReport
```

`check_dependencies()` 首先产生静态报告，不连接数据库。`pipeline.generate_case()` 只接受根部结果关系为 `proved` 的报告，并默认要求目标入口有效。随后 `main.prepare_case()` 编译完整查询，`executor.run_case()` 在建立数据库连接前调用 `validate_dependency_case()` 检查阶段链、图/IR/绑定/编译产物及必要状态检查。完整执行后，`attach_runtime_evidence()` 将运行报告与同一图、查询和目标摘要绑定；`evidence_context` 参数也可接收这种已经绑定的独立报告。

静态分析沿 AST 祖先、实际定义—使用边和结果表生产—消费边传播。报告保留 `analysis/semantic_propagation/ancestor_steps/scope_binding_edges/row_consumption_edges`，并使用逻辑解释器记录消费方式和实际输入。模式注册版本在 M2 的 PatternSpec 与实例中保存；不是每个传播步骤都各存一个独立版本字段。

| 层次 | 需要计算的内容 | 结果类型 |
|---|---|---|
| 值依赖 | 子表达式的值、NULL、行多重集或路径观察能否影响消费者 | `value_dependency=used/unused/conditional/unknown`；另以 `result_relation=proved/refuted/unknown` 记录两侧结果关系，两个字段不能互相替代 |
| 逻辑需求 | 消费者需要全部行、一个见证、前 k 行，还是已被决定性条件屏蔽 | `full / witness / bounded / masked / unknown`，附逐绑定前提 |
| 调用多重性 | 相关输入域和重复输入行如何使局部逻辑求值被请求 | 精确值或上下界、依赖键；物理调用次数另记未知 |
| 跨 Pattern 影响 | 内层变异是否改变外层规则的前提、见证域、暴露或可观测目标 | 每个实例独立保存 `premise_status=satisfied/refuted/unknown` 与 `exposure_status=exposed/masked/conditional/unreachable/unknown` |

例如 COUNT 通常观察输入行数；EXISTS 只观察空/非空；LIMIT 需要输出顺序或同值投影合同；WITH 必须区分普通投影、过滤、DISTINCT 以及聚合；OPTIONAL 必须逐输入行判断 NULL 扩展。逻辑 trace 区分 `values/count/exists` 需求模式：无 DISTINCT 的 COUNT 不请求其直接结果表的未使用 RETURN 值，EXISTS 也不请求 DISTINCT 投影值；COUNT DISTINCT 保留可能影响行数的值。WITH 从后继匹配、WHERE 和有效返回列逆向追踪活跃别名。未被消费的投影标记为 `unobserved_projection`，整个目标因此受外层屏蔽时 `target_valid=False`；短路内部保护仍单独报告。上述内容约束语义和逻辑需求，不意味着物理计划一定按语法顺序求值。

对于 m 行相关输入，逻辑请求数可能为 m；数据库可能去相关、缓存或物化。当前 evidence adapter 保留完整运行结果、原始 plans、数据库版本和计时摘要，但尚未实现可靠的计划算子到 AST 位点映射，因此 `physical_plan_mapping/physical_invocations` 均保持 `unknown`。

输入与候选计数明确区分图范围。节点数不超过5000时，M6 在完整 GraphSpec 上解释；更大图必须有增长证书，并使用 `validation_graph_spec`，标记 `input_observation_scope=certified_proof_graph`。压缩模型观察值保存在 `proof_model_input_*`、`proof_model_logical_request_interval`、`proof_model_*_candidates` 等字段中；它们不作为全图输入次数或候选总量。缺少全图数量证书时，全图相关区间为 `[0, None]`。只有常量决定值或 M3 的增长稳定标量证书能够支持全图屏蔽时，逻辑需求才可确定为0。目标入口存在性可由 M3 保留见证支持，`target_valid_scope` 与 `target_entry_evidence` 同时说明其范围，不能据此推出全图精确输入基数。

跨 Pattern 检查区分登记实例与被识别的上下文。`context_patterns` 能识别祖先或别名使用链上的 S7、S1/S2、S3/S4、EXISTS、LIMIT 等消费关系；对应 P4/P5/P13 或 S6/S8 若未重新建立完整规则前提，会保留 `unknown`。`cross_pattern_effects` 中非目标登记实例也保留未知，当前单端口实现不声称完成任意多实例联合验证。

共享读取分析覆盖关系类型、节点域、属性域及实体别名来源。不同标签名本身不证明节点域不相交；可用完整图事实计算交集，不完整读取保留区间与未知物理影响。

语义前提不成立或语义传播为 unknown 时，该用例不能作为已经通过等价性检查的正式性能用例；实际结果恰好相等也不替代缺失的规则依据。逻辑需求、调用规模或物理映射为 unknown，而语义关系已经成立时，可以保留用例并记录其未决维度。

运行时依次执行：加载并检查 G；完整执行两查询并比较有类型结果多重集；取得目标数据库版本与可用计划；按现有测量配置进行预热、成对随机顺序测量和独立确认。图状态检查失败、查询出错、结果不完整或结果不等时，不进入普通性能比较；超时保留独立分类及已有参考验证证据。计划证据与非分析模式计时分开保存。完整查询差异成立但局部算子无法映射时，可以记录完整查询性能候选，局部性能传导仍为 `unknown`。

ValScope 的借鉴边界是“定位修改点 → 沿实际使用依赖逐层传播摘要 → 到达根部”。原论文 §4.4、Algorithm 1（论文页码 394–395）传播的是语义近似关系，不是本系统的性能摘要。来源：[ValScope 原论文](https://www.usenix.org/system/files/osdi26-lin-li.pdf)。本节静态需求与工作检查由 GraphSymPerf 实现，物理局部归因仍待可靠映射能力支持；不能将其归为 ValScope 已证明的性能定理。

## 6. Pattern 目录、变异与图需求

当前论文目录由 `Src/paper_patterns.py:22–45` 定义：P1–P13 和 S1–S10。旧目录由 `Src/mutator.py:16–43` 定义：P1–P16 和 S1–S10。新记录保留 `catalogue=paper-v1` 与 `method_version=ast-pattern-v1` 两个独立字段；方法版本不是 Pattern 编号版本。

以下为当前 PatternSpec 的规则合同及对应图需求。全部23个论文标识均进入共享六阶段入口；是否接受某个随机宿主仍取决于具体结构、图约束和预算。具体规模分配由联合求解器完成，不能把某一组通过的样例推广为任意上下文均可求解。

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
| S6 `bounded_demand` | S6 | 需求已满足时替换后续结果表 L→H | 每个输入绑定的保留见证数达到 k；当前保守合同使用同值投影和流式分支；保留 LIMIT envelope |
| S7 `coalesce` | S7 | 已有非 NULL 前缀时替换后续参数 L→H | 前缀中更早参数为 NULL，决定参数非 NULL；0、false、空字符串均非 NULL；L/H 类型相容 |
| S8 `existential_witness` | S8 | 已有见证时替换其他候选区域 L→H | 每个实际输入绑定有保留见证；EXISTS 仍为 TRUE；候选备选可改变局部多重集 |
| S9 `empty_input` | S9 | 空必选输入后的独立读取 L→H | 精确空输入和合法下游接口；不跨会重新产生行的聚合；不把 OPTIONAL 空分支当作零输入 |
| S10 `membership` | S10 | 空集合语义外壳中的成员读取 L→H | 保持决定性空输入、NULL 安全成员谓词与 DISTINCT 外壳；当前表达不等于任意原生集合算子 |

旧 P16 是量词输入组顺序，不在当前论文目录内。迁移时保留历史身份，不将它无说明地塞入论文 P13 或任意 S 模式。

## 7. 语义说明例与阶段映射

以下沿用方法稿的 P3/S7 具体图例，说明阶段之间必须保持的事实与结果关系。真实生成器还会选择其他宿主、局部符号和图域；一次运行的具体 IR、图与检查由格式4归档记录，不把说明图例当作固定整图配方。

### 7.1 P3：随机 COUNT 宿主中的路径镜像

M1 可能随机生成如下宿主。`ROW_HOLE` 是结果表端口，外层 COUNT 是已经确定的结构，不因选择 P3 而追加。

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

M1 已确定外层查询及整数标量端口，M2 选择 S7 的原生标量形式；其 imports 为空，决定项由符号字面量表达。M3 将决定项绑定为0，构造 m>0 个 Root 和独立的 WorkRoot(id=1)，后者有 N>1 个 HIGH 邻居；工作节点不带 Root 标签。低工作参数是常量0。M4/M5 得到：

```cypher
// Q_L
MATCH (u:Root)
RETURN coalesce(NULL, 0, 0) AS value
```

```cypher
// Q_H
MATCH (u:Root)
RETURN coalesce(NULL, 0, COUNT {
  MATCH (r:WorkRoot {id: 1})-[:HIGH]->(v)
}) AS value
```

两侧均返回 m 份整数0，0是非 NULL 值。低工作参数为0，高工作子查询计数为 N；结果关系从 coalesce 外壳开始传播。实际生成还可以加入已声明的等价标量层，但不能在 M4 临时增加结构。本例说明当前没有节点导入的 native guard 变体；宿主 u 不会被工作子查询隐式捕获。

如果将决定项改为 NULL，则两侧可能分别返回0和 N，不能沿用同一短路前提。决定项、查询和图的任何改变都会使已保存的绑定或阶段摘要失效。宿主其他读取若能访问新增 HIGH 节点，M3 必须联合验证，不凭不同变量名推断图域隔离。M6 只把 mN 记录为假定逐输入枚举的潜在规模，不把它写成物理调用量或耗时下界。

### 7.3 嵌套 Pattern 的两种状态

当登记目标 P3 位于宿主原有 `coalesce` 的 fallback 中，M6 可将该外层识别为 S7 上下文。前缀非 NULL 时，外层记录 `registration=recognized_context/premise_status=satisfied/logical_demand=masked`；P3 的根部结果关系可保持 `proved`，但目标入口被屏蔽，`target_valid=false`。默认编排要求入口有效，会记录失败并尝试其他宿主；显式关闭该要求时可保留分类用例。被识别的外层 S7 不因此变成第二个登记变异目标。

以 S7 为登记目标时，其完整 coalesce envelope 被到达而内部备选工作需求为0，仍是有效短路测试。报告用 `protected_work_masked` 区分这种情形与整个目标入口被外层屏蔽。改变前缀为 NULL 会改变决定性前提，需要重新构图、实例化和检查，不能在同一计时用例中改写原图摘要。

## 8. 编排、失败与预算

`pipeline.generate_case(pattern, scale, seed, data_seed=0, *, options=None)` 为六阶段编排入口。`main.prepare_case(..., method="ast-pattern-v1", ast_options=None)` 默认调用它，再编译目标语言。常规配置的 `generation.method` 默认也是 `ast-pattern-v1`；`legacy-v1`、`symbolic-v1` 只能显式选择，后者要求 `generation.composition`。

```python
# 当前公共用法；prepare_case 此时只生成与编译，不连接数据库。
from main import prepare_case

case = prepare_case(None, "P3", 100, 0, 3, ast_options={
    "max_attempts": 64,
    "solver_budget": {"terminal_assignments": 32, "max_nodes": 200000},
})
```

编排按 M1→M2→M3→M4→M5→M6 保存阶段记录，每个记录包含 `stage/version/input_digest/output_digest/data`。新方法不接受旧 GDsmith 具体查询 seed、composition、legacy complexity 或 state_variant 控制；本地规则形状选项使用 `generation.ast.pattern_options`，宿主语法使用 `generation.ast.grammar`。可用配置模板为 `configs/ast-pattern.yaml`。

`generation.ast` 当前支持 `grammar/capabilities/ast_budget/embedding_budget/solver_budget/max_attempts/pattern_options/host_seed/embed_seed/require_target_entry`。默认 `max_attempts=64`，`require_target_entry=true`。每次尝试使用独立派生种子；失败保留阶段、代码、原因、证据和已完成阶段记录。根部语义未证明或要求的目标入口不可达时不返回接受用例。搜索预算控制可接受的宿主重采样次数，不放宽类型、结果关系或目标暴露条件。

| 当前状态或失败出口 | 含义与控制 |
|---|---|
| `generation_exhausted` | M1 预算内无法完成合法宿主，有限尝试可换宿主 |
| `no_compatible_site`、`interface_mismatch`、`embedding_budget_exhausted` | 端口/局部规则不相容或局部节点预算不足，记录后重试 |
| `constraint_conflict`、`budget_exhausted` | 终端域冲突或有限联合构图搜索未找到状态，不解释为任意图上无解 |
| `unsupported_fragment` | 支持片段之外的结构，包括多个未填端口；保留拒绝原因 |
| `invalid_configuration`、`invalid_budget`、`unknown_pattern` | 配置错误立即抛出，不消耗重试伪装成随机失败 |
| `contract_drift`、`unbound_slot`、`unexpanded_structure`、`out_of_region_mutation` | 实现或阶段输入合同破坏，立即失败，不补默认值或回退旧路线 |
| `premise_failed` | M5 的适用前提或验证未通过；保留失败并按生成预算重试 |
| `semantic_relation_unknown`、`target_unreachable` | M6 根部关系或默认入口要求未满足，不进入正式性能比较 |
| `generation_budget_exhausted` | 全部允许尝试耗尽，返回完整失败轨迹 |
| 运行时结果不等、出错、未完成或超时 | 由现有执行协议单列；不能当作普通已确认性能差异 |
| 需求或物理映射未知 | 属于 DependencyReport 的状态字段，当前并无同名 `work_dependency_unknown/plan_mapping_unknown` 异常码 |

M1 预算为 `clauses/nodes/expression_depth/subquery_depth/path_hops/port_nodes/match_edges/projections/expression_arity`；各递归分支共享节点账本。M2 可用 `embedding_budget.max_local_nodes` 进一步收紧端口预算。M3 当前接受 `terminal_assignments/max_nodes/max_edges`，其他 solver budget 键被拒绝；共享语义求解器还受既有有限搜索上限约束。`unsatisfiable_in_domain` 是可保留的未来精确出口，当前实现主要返回带原因的有限预算失败，不宣称全域无解。

## 9. 版本与回放

运行记录使用 `method_version=ast-pattern-v1`、`grammar_version=typed-host-v2`，M2 记录 `paper-v1/local-law-v1` 注册表与规则身份。语法版本区分扩展前后的选择集合、随机轨迹和预算；同一个种子跨语法版本不承诺产生同一宿主。`source_version()` 保存 Git 提交、工作树状态、全部 Python 源文件指纹及 Python 版本。文档修订号不参与方法运行身份。

当前新用例使用归档格式4，保存以下实际文件：

```text
result.json               # 归档版本、输入/扩展摘要、case、测量配置与结果
setup.cypher
query_a.cypher            # 或目标语言对应后缀
query_b.cypher
M1-host.json              # 含 clause_skeleton、scope_graph、expansion_trace
M2-embedding.json
M3-construction.json      # 阶段数据包含 graph_spec、binding_plan 与构造证书
M3-state.json
M3-bindings.json
M4-instantiation.json
M5-pair.json
M6-dependencies.json
M6-runtime.json
```

子句骨架与展开轨迹当前内嵌于 M1 文件，并未额外写 `M1-clause-skeleton.json/M1-expansion.json`。`result.json` 承担先前设计中 manifest 的职责，查询文件也未放入独立 `queries/` 子目录。M1–M6 文件保存各自完整阶段记录，M3 state/bindings 与 M6 runtime 额外按实际内容写出；`read_case()` 要求文件存在且与归档内容一致，禁止格式4向旧格式降级。

回放分为两个已实现入口：

1. **执行回放**：`main.replay()` 直接读取归档 IR、参数、图和检查，必要时按目标与已验证特性重新编译后执行。新用例在重编译、保留不支持状态及跨目标回放时仍保留格式4与完整阶段合同。
2. **生成再现**：`pipeline.reproduce_generation(case)` 先要求当前 Python 源码指纹与归档匹配，再按归档选项和种子重跑 M1–M6，逐阶段比较摘要；出现变化时报告首次不一致阶段，不覆盖原记录。

`validate_stage_records()` 检查摘要链、M2 对 M1 的消费、M4 源到 M5 的一致性，以及公共 case 中的图、查询、方向、side roles、构造和依赖报告。执行前还检查必要状态查询未丢失、编译文本和编译器 bindings/列名/特性与 IR 相符。旧格式1–3保留对应解码路径，旧记录不会自动标成新方法。

## 10. 方法—接口—测试—证据追踪

下表列出当前实现和实际测试入口；冻结执行结果统一登记在 [实现验收记录](ast-pattern-implementation-status.md)，测试文件的存在本身不代表某个任意上下文已受支持。

| 方法 ID | 当前接口/模块 | 已落实的合同 | 归档证据 | 当前测试入口 |
|---|---|---|---|---|
| M1 | `host_generator.generate_random_ast` | 两层随机生成独立于 Pattern/图，类型、作用域、可空性、能力与预算校验 | M1 HostAst 内的骨架、scope、trace、shape/host digest | `tests/test_ast_pattern_host.py`；`tests/test_ast_pattern_contracts.py` |
| M2 | `embedding.embed_patterns` | 一个类型化端口、局部结构固定、alpha rename、端口外结构不变 | M2 EmbeddedAst、origin map、PatternSpec/instance | `tests/test_ast_pattern_embedding.py` |
| M3 | `state_builder.construct_graph_state` | 固定 AST 的需求分解、有限联合终端赋值、同图及证书冻结 | M3 图、BindingPlan、构造记录、population 约束与证据 | `tests/test_ast_pattern_state.py`；共享 `test_requirement_domains/test_truth_contracts` 回归 |
| M4 | `instantiation.instantiate_query` | 全树终端填充，不调用变异器或结构生成器，图/计划/证书防漂移 | M4 source/replacement/bindings/checks 摘要 | `tests/test_ast_pattern_state.py`；`tests/test_ast_pattern_contracts.py` |
| M5 | `mutation.mutate_target` | 指定目标局部变异，保留实际语义与加载检查，角色方向明确 | M5 两侧 IR、mutation_record、必要检查 | `tests/test_ast_pattern_state.py`；`tests/test_ast_pattern_runtime.py`；原规则拒绝与语义回归 |
| M6 | `dependency_check` 与 `evidence_adapter` | 根部关系、输入范围、需求、上下文、共享读取、未知物理映射分别报告 | M6 static certificate 与 bound runtime report | `tests/test_ast_pattern_dependencies.py`；`tests/test_ast_pattern_runtime.py` |
| 编排/回放 | `pipeline`、`main.save_case/read_case` | 默认新入口、无隐式回退、阶段摘要链、格式4、源码一致生成再现 | result.json 及 M1–M6 独立文件 | `tests/test_ast_pattern_pipeline.py` |
| 实际 Neo4j | `tests/test_ast_pattern_neo4j.py` | 加载后状态检查、完整结果、运行证据、归档回放及 N=10000 证书状态 | 独立测试结果与真实用例归档 | 25种规则/形式×2 seed、2项运行回放、3项大状态检查 |

旧 `tests/test_symbolic_pipeline.py`、`tests/test_all_paper_patterns.py` 等已经明确调用兼容入口，继续检验历史规则及归档，不计作新 M1/M2 的直接实现证据。新 `test_ast_pattern_pipeline.py` 另行检查默认入口和全部23个论文标识。

### 10.1 端到端合同与扩展边界

| 验收用例 | 当前合同或扩展范围 |
|---|---|
| 同一宿主、多个兼容 Pattern | M1 归档一致；M2 选择不同局部规则；外部结构保持 |
| 同一 Pattern、不同宿主和深度 | 不调用完整 Pattern 模板；覆盖结果表、谓词、标量等允许接口 |
| 多个同形局部规则位点 | 局部结构显式 mark 选择目标；多个宿主显式 Port 当前被拒绝 |
| P3 的 N 参数族 | 模型上 N+4 节点、N+3 边、N+1 候选、1 个通过；两查询 COUNT=1；目标计时另行记录 |
| P3 的顺序敏感路径消费 | 返回路径节点序列等不满足反转不变的观察时拒绝，不能仅看外层 COUNT |
| S7 的 answer=0 与 answer=NULL | 0 接受为非 NULL 决定值；NULL 情形不能沿用同一前提 |
| S6/S8 逐绑定见证不足 | 某输入绑定缺见证时拒绝，不能用总见证数代替每绑定事实 |
| P11 的四种匹配组合 | both/left-only/right-only/neither 逐输入域保持 NULL 扩展与行多重性 |
| 冲突外层 COUNT 区间 | 返回有来源的约束冲突/预算未知，不改阈值、不额外加成功行 |
| 目标与外层读取共享实体域 | 分析节点/关系/属性及别名来源；多个显式登记 Pattern 的通用联合求解仍为扩展 |
| 外层原有 S7 内嵌 P3 | 区分根部等价、外层前提与 P3 入口屏蔽；默认要求入口有效时重试 |
| m 行相关输入 | 逻辑输入多重性可核验；无计划证据时物理调用次数保持 unknown |
| 目标优化器消除两侧差异 | 记录无差异或目标擦除变换；不将生成成功当作漏洞 |
| 损坏 Graph State/绑定/位点 | 各阶段摘要与前提校验拒绝继续执行 |
| 旧编号与旧归档回放 | 旧 P11/P13 等身份保持；不会转换成错误论文 Pattern |
| 短路比较方向 | L→H 的 b 变慢与 H→R 的 a 变慢均按各自角色进入筛选；不能统一要求 a 变慢 |

### 10.2 记录与统计口径

运行记录保留每次失败及阶段轨迹，当前 run.json 记录方法、各 Pattern 的尝试/执行数、停止原因及生成失败。阶段数据可用于离线计算子句/结构种类、端口/深度分布、嵌入与构造接受率；这些派生指标尚未全部成为自动汇总列。结构摘要保留布尔操作符、函数名、方向、path mode、OPTIONAL/DISTINCT 标志及子节点顺序，只忽略终端与重命名；不能用叶子取值变化代替结构增长。

正确性证据由阶段合同、状态查询、完整结果检查与回放摘要提供。若据此汇总覆盖率、失败率或屏蔽分布，必须保留分母与失败样本口径。当前物理计划映射保持未知，不能汇总成“已证明局部物理传播”的数量。

性能测试复用现有成对测量与确认阈值，并归档实际配置。报告运行时间比、绝对差、方向一致性及确认结果；DB Hits、行数和算子循环数用作机制证据。本文不填写尚未测量的数值，也不将静态预测的工作增长写成运行时间复杂度结论。

### 10.3 GDsmith 借鉴部分的新增合同测试

以下为本轮已落实到阶段测试与共享语义回归的合同分组；它们是测试索引，不是额外的10次数据库运行。完整冻结结果见第12.3节和实现验收记录。

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

GD1–GD5 主要对应 `test_ast_pattern_host/embedding/contracts`；GD6–GD10 对应 `test_ast_pattern_state` 及 `test_truth_contracts/test_requirement_domains` 的共享语义回归。实际输入域验证、NULL 前缀反例和压缩图证据范围另由 `test_ast_pattern_dependencies` 检查。尚未自动汇总的覆盖率按第10.2节口径从实际记录计算。

## 11. 默认入口与保留的扩展要求

默认 `prepare_case()` 与 `run()` 已切换到六阶段方法；新路径失败返回结构化原因，显式兼容方法负责历史生成和回放。旧配置若要继续使用 composition 或有限模板，需要明确填写对应 `generation.method`。默认配置不把 GDsmith 具体查询日志当作 M1 输入。

本轮实现以一个登记目标、现有有界 IR 与 Neo4j 显式图加载为验收范围。仍需单独扩展的能力包括：多个显式端口/实例的联合构造，任意已有子树替换，更多目标数据库的 v2 状态加载，以及可信的物理计划到 AST 位点映射。扩展这些能力时继续使用局部 Pattern 合同、全树共享需求和阶段证据，不改变六阶段方法顺序。

UNWIND、ORDER BY、SKIP 尚未加入普通宿主语法；它们需要查询表示、作用域、解释、图数量约束及依赖传播共同支持。普通宿主中的任意变长路径与单 MATCH 多边路径也不属于本轮扩展。新增候选采用已有可解释的子句和结果表组合，不以仅能渲染语法文本代替六阶段支持。

实现与方法的一致性通过 M1 宿主、M2 插入、M3 联合状态、M4 全树赋值、M5 单点变异、M6 依赖及执行证据逐步检查。字段存在或某个测试通过不代替未接入能力的声明。

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

`gdsmith-r1` 是本轮实施前的文档修订：当时核对 GDsmith 正式论文及固定提交源码，补充 M1 两层生成、M3 固定 AST 的需求分解和 M4 终端物化边界。该次修订本身没有实现新接口，其历史验证范围与下述本轮实现分开记录。

### 12.3 `implementation-r1` 的实现验收

2026-10-26 本轮已实现并接入第2.4节所列模块，技术文档同步为当前接口、数据位置、阶段控制、格式4和回放行为。主要新验收入口为8个 `test_ast_pattern_*.py` 文件及 `test_exists_growth_certificate.py`，旧语义与兼容路线继续回归。

最终冻结执行结果为：完整测试 **2938 passed、1532 skipped**；新验收文件中有 **297 项离线测试通过**，既有归档字段回归也覆盖新增元数据。显式临时 **Neo4j 2026.09.0** 上，新集成套件 **56 passed**：25种论文规则/形式×2个查询 seed，共50项加载与完整结果检查；2项真实运行、归档及回放；4项 N=10000 大状态检查（P3/P10/S7/S8）。跳过项不计作通过，集成结果也不表示任意 AST、任意数据库版本或其他后端已经验证。

P3/S7 的实际 CLI 用例、跨进程生成再现、真实 CLI 执行回放及冻结源码/测试产物位置保存在 [实现验收记录](ast-pattern-implementation-status.md)，机器可读摘要见 [验收摘要](ast-pattern-acceptance-20261026.json)。性能 smoke 的 `no_candidate` 表示该配置下未形成性能候选，不是缺陷发现结论。局部物理映射和调用次数继续保持 unknown。

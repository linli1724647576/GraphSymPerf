# GraphSymPerf 查询对称重写代码级方案（讨论稿 v0.1）

> 状态：供方案讨论，不代表当前仓库已经实现。
>
> 研究边界：同一个任意固定图状态 \(G\) 上的查询对称族。本文不要求 \(G\) 具有自同构，不构造双拷贝对称图，不把布尔冗余或普通等价重写作为正式贡献。

## 1. 结论

推荐把 GraphSymPerf 的核心测试单位从“等价查询对”收紧为：

\[
\mathcal{F}(Q,G,\mathcal R)
=
\left(
G,
H_Q,
\Gamma_{\mathcal R},
\{Q_\gamma\}_{\gamma\in O_{\mathcal R}(Q)},
C_{\mathcal R},
E_\mathcal{F}
\right).
\]

其中：

- \(G\) 是一次加载后保持不变的任意属性图；
- \(H_Q\) 是查询 \(Q\) 的图模式中间表示；
- \(\mathcal R\) 是参与该查询族构造的一条或多条结构化对称规则；
- \(\Gamma_{\mathcal R}\) 是这些规则在具体查询位点上通过认证的表示对称生成元集合；
- \(O_{\mathcal R}(Q)\) 是生成元及其合法复合作用在 \(Q\) 上得到的查询表示轨道；
- \(C_{\mathcal R}\) 是覆盖全部生成元及其复合条件的联合适用证书；
- \(E_\mathcal{F}\) 是查询族级结果与运行时证据。

这一路线与微信原始文档一致：对称性施加在查询表达及其图原生语义上，所有成员在同一个图状态上执行。图状态对称只是一种可选的受控实验设计，不是查询对称重写成立的前提，也不应进入本项目正式主线。

建议把论文贡献限定为三项：

1. **图模式表示的结构化对称族**：从同一语义图模式的合法表示轨道生成查询族，而不是任意等价重写。
2. **规则适用证书**：对每个具体查询、具体位点和具体对称动作保存可复核的语义前提与方言前提。
3. **查询族级性能不一致预言**：不只比较一对查询，而是利用同一对称轨道内多个成员的稳健中心、离群成员和复验方向判断优化器对表示形式的异常敏感性。

## 2. 为什么图状态不需要对称

微信文档的基本关系是：

\[
Q \equiv T(Q)
\Longrightarrow
\operatorname{Result}_G(Q)
=
\operatorname{Result}_G(T(Q)).
\]

这里 \(T\) 作用于查询，\(G\) 在两次执行之间保持不变。只要变换来自查询语言语义，例如无向关系模式的端点表示反转，等价性对任意满足该语言语义的图状态成立。

以无向单边模式为例：

```cypher
MATCH (a:User {id: 123})-[r:FOLLOWS]-(b:User)
RETURN b
```

与：

```cypher
MATCH (b:User)-[r:FOLLOWS]-(a:User {id: 123})
RETURN b
```

变量名、标签、属性约束、关系变量和投影均保持原语义角色，只改变无向模式的表面线性化起点。两条查询在任意同一个 \(G\) 上应产生相同绑定多重集。这里不需要 \(G\) 存在任何节点置换或自同构。

要求图状态先对称会引入三个偏差：

1. 测试对象从“优化器是否对查询表示敏感”变成“系统是否对人工同构数据副本敏感”；
2. 双拷贝、轨道锚点和保留身份属性会改变标签频率、度数分布、索引统计和物理装载；
3. 候选只能在特殊构造图上成立，削弱对任意真实图状态和 GDsmith 图状态的适用性。

因此，随机图生成后再做“变换—剪枝—保留对称图”不适合作为正式生成路线。图剪枝仍然有价值，但应放在候选约简阶段，用于缩小复现图，同时保持同一个查询族的症状。

## 3. 与现有工作的边界

### 3.1 GDsmith

GDsmith 已能生成语义有效、较可能非空且行为多样的 Cypher 查询及配套图状态。GraphSymPerf 应继续复用其图—查询联合生成，不重新实现通用查询生成器。

复用方式：

- GDsmith 负责生成 \(G\) 和只读种子查询 \(Q\)；
- GraphSymPerf 解析 \(Q\)，判断是否存在可认证的图模式表示对称；
- 不修改 \(G\)，只生成 \(\mathcal{F}(Q,G,r)\)；
- 若 \(Q\) 不在支持域内，使用原因码拒绝并继续生成。

参考：[GDsmith: Detecting Bugs in Graph Database Engines](https://arxiv.org/abs/2206.08530)。

### 3.2 GRev

GRev 是最接近的工作。它把等价查询重写（Equivalent Query Rewriting, EQR）用于图数据库测试，通过抽象语法图（Abstract Syntax Graph, ASG）编码查询语义，再用随机游走覆盖（Random Walk Covering, RWC）生成多条等价查询。论文已经同时考虑结果不一致与运行时间差异，并报告了性能问题。

因此，以下内容不能作为 GraphSymPerf 的新贡献：

- 首次把 EQR 用于图数据库；
- 首次从一个图查询生成多个等价查询；
- 首次比较等价图查询的运行时间；
- 仅用不同 pattern 线性化触发不同计划。

可以复用或对照的内容：

- 将 GRev 作为基线生成器；
- 参考 ASG 的“图模式语义表示—多种线性化”分层；
- 用同一批 GDsmith 图和查询比较 GRev 与 GraphSymPerf 的有效查询族产率、查询族规模、规则可解释性和候选复验率。

不建议直接把整个 GRev 工程嵌入主线：

- 其公开仓库是完整 Docker/ELK/多数据库测试栈，而不是稳定的纯重写库；
- 当前公开仓库页显示的历史较浅，复用接口和维护边界需要源码审计；
- 当前公开页未显示明确许可证，复制源码前必须单独确认许可。

若后续许可允许，最多抽取为外部基线适配器，不让 GRev 的运行协议替代 GraphSymPerf 的证书、结果门禁和 runtime-first 验证。

参考：[GRev 论文](https://joyemang33.github.io/assets/pdf/GRev.pdf)；[GRev 代码仓库](https://github.com/CUHK-Shenzhen-SE/GRev)。

### 3.3 Gamera

Gamera 的价值在于图原生蜕变关系（graph-aware metamorphic relations）的分类与图结构敏感测试。其目标主要是逻辑缺陷，关系形式不等于“同一语义图模式的表示轨道”。建议把 Gamera 用作：

- 图原生规则设计的相关工作；
- 逻辑缺陷测试基线；
- 边、路径和复合模式边界用例的来源。

不建议把 Gamera 直接作为正式查询对称族生成器。

参考：[Gamera 论文](https://www.vldb.org/pvldb/vol17/p836-zhuang.pdf)；[Gamera 代码仓库](https://github.com/cuhk-seclab/Gamera)。

### 3.4 AMOEBA 与 Query Plan Guidance

AMOEBA 已建立“同一数据库上等价查询的响应时间差异可暴露性能问题”的关系数据库先例。因此 GraphSymPerf 的增量不能只是把该思想移植到 Cypher，而必须依赖图模式表示、规则证书和查询族预言。

Query Plan Guidance（QPG）通过修改数据库状态探索更多查询计划。它说明图状态会影响计划覆盖，但其状态变异目标与本方案不同。GraphSymPerf 的正式候选判定不应由计划指导；计划只在 runtime 候选通过独立复验后用于解释。

参考：[AMOEBA](https://arxiv.org/abs/2105.10016)；[Query Plan Guidance](https://arxiv.org/abs/2312.17510)。

## 4. 领域模型

### 4.1 查询模式中间表示

新增深模块：

```text
src/graph_sym_perf/query_symmetry/
├── models.py
├── pattern_ir.py
├── analyzer.py
├── group_action.py
├── certificate.py
├── renderer.py
├── family_builder.py
├── rules/
│   ├── base.py
│   ├── undirected_linearization.py
│   ├── pattern_component_permutation.py
│   ├── bounded_path_endpoint.py
│   └── mandatory_match_permutation.py
└── serialization.py
```

`PatternIR` 不保存某一种文本顺序，而保存图模式语义事实：

```python
@dataclass(frozen=True)
class PatternIR:
    node_atoms: tuple[NodeAtom, ...]
    relationship_atoms: tuple[RelationshipAtom, ...]
    path_atoms: tuple[PathAtom, ...]
    predicates: tuple[PredicateAtom, ...]
    scopes: tuple[ScopeFrame, ...]
    projections: tuple[Projection, ...]
    order_contract: OrderContract
    multiplicity_contract: MultiplicityContract
```

关键字段：

- `NodeAtom`：变量、标签集合、静态属性约束、所在作用域；
- `RelationshipAtom`：关系变量、类型集合、方向、左右端点、长度约束；
- `PathAtom`：路径变量、组成关系、端点、是否对外可观察；
- `PredicateAtom`：引用变量、确定性、总性、空值行为；
- `ScopeFrame`：`MATCH/WITH/RETURN` 的变量定义与使用；
- `Projection`：结果列身份、聚合、`DISTINCT`、路径和实体可观察性；
- `OrderContract`：有序序列或无序多重集；
- `MultiplicityContract`：关系唯一性、重复项和路径重复度。

`PatternIR.semantic_hash` 必须：

- 不受允许的模式表示顺序影响；
- 保留变量的语义角色，而不是把所有变量任意交换；
- 保留方向、标签、类型、属性、作用域、投影和重复度事实；
- 可用于判断所有查询族成员是否仍表示同一语义对象。

### 4.2 对称动作

```python
@dataclass(frozen=True)
class SymmetryAction:
    action_id: str
    generator_ids: tuple[str, ...]
    site_ids: tuple[str, ...]
    permutation: tuple[int, ...]
    orientation: tuple[str, ...]
    action_word: tuple[str, ...]
```

对称动作不是任意 AST rewrite。它只能改变 `PatternIR` 的表示方案：

- 从哪个节点开始线性化一个无向模式；
- 以何种方向书写无向关系；
- 同一 `MATCH` 内的 pattern component 以何种顺序出现；
- 在依赖关系允许时，以何种顺序呈现 mandatory `MATCH`；
- 对隐藏路径值的有限无向路径选择哪个端点作为文本起点。

动作应用后必须满足：

\[
\operatorname{semanticHash}(\gamma\cdot Q)
=
\operatorname{semanticHash}(Q).
\]

允许动作由多个已认证生成元复合形成，但不能仅凭“每个生成元单独安全”推出复合安全。联合
证书必须证明：生成元共享同一语义不变量；前序动作不会使后续动作的适用前提失效；重叠
位点不存在未建模冲突；或者对动作词的每个前缀重新执行适用检查。生成元不要求两两交换，
但每个实际枚举的动作词都必须可重放和可验证。

### 4.3 结构化查询族

```python
@dataclass(frozen=True)
class QueryFamilyMember:
    member_id: str
    query: str
    action: SymmetryAction
    presentation_hash: str


@dataclass(frozen=True)
class CertifiedQueryFamily:
    family_id: str
    seed_query: str
    semantic_ir_hash: str
    rule_ids: tuple[str, ...]
    generator_ids: tuple[str, ...]
    members: tuple[QueryFamilyMember, ...]
    certificate: RuleApplicabilityCertificate
    orbit_status: Literal["complete", "bounded_sample"]
    parameters: Mapping[str, object]
```

约束：

- `CertifiedQueryFamily.members` 必须至少包含 4 个语义等价、表面表示不同的成员；
- 正式查询族既可来自单条规则的多个生成元，也可来自多条规则的已认证生成元复合；
- 多生成元族必须保存完整 `rule_ids`、`generator_ids` 和每个成员的 `action_word`，不能将
  组合族伪装成单规则实例；
- `rule_ids` 和 `generator_ids` 使用规范顺序写入身份哈希；动作应用顺序只由
  `action_word` 表达，避免同一族因序列化顺序不同获得多个身份；
- 去重后只有 2–3 个成员的候选只能进入开发诊断或历史 pair 兼容路径，标记为
  `DIAGNOSTIC_FAMILY_CARDINALITY`，不得计入正式查询族、核心产率或查询族级性能预言；
- 同一 `family_id` 的所有成员共享 `semantic_ir_hash`；
- `presentation_hash` 必须不同，避免把空白或大小写变化计为成员；
- 轨道过大时使用确定性有界采样，并记录未枚举规模上界和采样种子。

### 4.4 规则适用证书

```python
@dataclass(frozen=True)
class RuleApplicabilityCertificate:
    schema_version: int
    family_id: str
    rule_ids: tuple[str, ...]
    rule_versions: Mapping[str, str]
    source_query_hash: str
    semantic_ir_hash: str
    site_ids: tuple[str, ...]
    generator_ids: tuple[str, ...]
    generator_certificate_digests: tuple[str, ...]
    preserved_facts: tuple[str, ...]
    composition_obligations: tuple[str, ...]
    scope_obligations: tuple[str, ...]
    observation_obligations: tuple[str, ...]
    multiplicity_obligations: tuple[str, ...]
    dialect_obligations: tuple[str, ...]
    capability_hash: str
    proof_id: str
    proof_digest: str
```

证书必须绑定“规则集合—版本—查询—位点—生成元—方言能力”，不能只是规则名或若干独立
证书的拼接。最低义务包括：

- 变量定义、使用和投影身份不变；
- 关系方向语义不变；
- 标签、关系类型、属性和参数不变；
- `OPTIONAL MATCH`、`WITH`、聚合和相关子查询边界未被跨越；
- 路径对象或关系列表未因反转而改变可观察值；
- 重复度和关系唯一性作用域保持；
- `ORDER BY/LIMIT/SKIP` 的有序语义未改变；
- 目标 GDBMS 声明支持所用语法和语义。

联合证书还必须证明：

- 所有生成元保持同一个 `PatternIR` 语义不变量；
- 生成元作用位点重叠时，其组合效果已建模且不会覆盖彼此的语义前提；
- 每个动作词的前缀都保持后续生成元的适用条件，或在应用时重新认证；
- 复合后的查询重新解析后仍具有相同 `semantic_hash`；
- 轨道的确定性枚举、去重和有界采样不会把不可认证动作混入正式成员。

证书验证采用 fail-closed：

- 明确违反义务：`UNSAFE_*`；
- 当前分析器无法证明：`UNKNOWN_*`；
- 方言不支持：`DIALECT_*`；
- 只有全部义务满足才产生查询族。

动态结果一致只验证当前 \(G\) 上的实际可观察行为，不能替代静态证书。

## 5. 第一批规则（已锁定）

第一批正式 CORE 注册表只包含以下两条规则。`CORE-1` 是主要的图原生表示对称规则；
`CORE-2` 用于扩展结构化轨道并压力测试图模式规划敏感性。其余规则保持
`EXPERIMENTAL` 或 `CONTROL`，未经后续独立决策不得进入正式 CORE 产率。

### 5.1 CORE-1：无向图模式线性化族

规则 ID：`undirected_pattern_linearization`

支持域：

- ordinary mandatory `MATCH`；
- 单边或固定长度无向链；
- 端点、关系变量和属性约束保留原变量身份；
- 不返回方向敏感的路径对象；
- 不跨作用域移动模式。

生成成员：

- 每个合法端点作为文本起点；
- 每条无向关系的左右表示反转；
- 对链模式生成合法的正向和逆向线性化。

这是最符合微信动机例子的图原生规则，当前仓库已有无向端点证明和有限 Cypher AST 基础，实现可行性最高。
它承担第一阶段关于“图模式表示对称”的主要方法证据和实现主线。
但单条无向边通常只有 2 个去重后的有效表示，不能单独构成正式查询族；该规则只有在一个
可认证位点的表示轨道至少产生 4 个不同成员，或与其他生成元通过联合证书后组成至少 4 个
成员的复合轨道时，才可进入正式路径。否则只保留为诊断候选。

典型四成员构造是在同一查询中找到两个可联合认证的无向模式位点 \(g_1,g_2\)，由
\(\{e,g_1,g_2,g_1g_2\}\) 产生四种表示。这里四成员来自两个真实结构动作的笛卡尔轨道，
不是通过空白、变量改名或布尔冗余补足数量。

### 5.2 CORE-2：同一 MATCH 内模式分量排列族

规则 ID：`match_pattern_component_permutation`

支持域：

- 同一个 mandatory `MATCH` 内的逗号分隔 pattern list；
- 每个分量都可降为当前关系代数表示；
- `WHERE` 保持附着在同一 `MATCH`；
- 不改变 Cypher 的关系唯一性作用域；
- 不包含未建模的子查询、模式推导式或动态标签。

生成成员：

- 对 \(k\) 个分量的排列轨道做确定性枚举；
- 当 \(k!\) 过大时使用覆盖首位置、末位置和相邻交换的有界样本；
- 证书记录分量依赖图和排列。

此规则比普通谓词交换更接近图模式 join-order 敏感性，但与 GRev 存在明显邻近关系，论文必须强调“完整族证据和证书”，不能把排列本身写成首创。
正式支持域严格限定为同一个 `MATCH` 的 pattern list 表示顺序：不得把它扩展成任意关系
代数 join-order 重写、跨子句重排或谓词重排。它在论文中的作用是提供结构化族扩展与
对照证据，不单独承担方法新颖性主张。
当 \(k=2\) 时只有 2 个排列，必须标记为诊断候选；正式查询族应选择 \(k\ge 3\) 的可认证
pattern list，并从其排列轨道中确定性生成至少 4 个成员。

### 5.3 EXPERIMENTAL-1：有限无向路径端点族

规则 ID：`bounded_undirected_path_endpoint`

只有在以下条件同时成立时进入试验：

- 有有限上下界；
- 路径变量和可变长度关系列表均不对外可观察；
- 端点变量身份不变；
- 返回结果不依赖路径内部顺序；
- 多重路径语义经方言测试确认；
- 静态关系代数证明通过。

未完成跨 GDBMS 多重路径语义测试前不得进入自动 issue promotion。

### 5.4 EXPERIMENTAL-2：mandatory MATCH 依赖许可排列

规则 ID：`mandatory_match_dependency_permutation`

需要先构建子句依赖图：

- 若后一个 `MATCH` 使用前一个子句新绑定变量，则不能任意交换；
- `WHERE` 必须随所属 `MATCH` 一起移动；
- `WITH`、聚合、`DISTINCT`、`ORDER BY`、`LIMIT` 和子查询形成硬边界；
- `OPTIONAL MATCH` 默认不可交换。

该规则最能压力测试跨 pattern join-order，但作用域风险最高，建议第二阶段实现。

### 5.5 CONTROL：不计为论文正式规则

以下规则只作负对照或解析器控制，不计入正式规则产率和核心 bug yield：

- 绑定变量一致性重命名；
- 同一有向边的等价箭头记法；
- 谓词顺序交换；
- 标签书写顺序交换；
- 空白、大小写和括号格式变化。

所有布尔冗余、恒真式注入、短路和一般表达式 EQR 从正式配置、正式规则注册表和正式统计中移除。历史 issue 与复现材料可保留，但必须标记为历史路线。

## 6. 查询族构造算法

```python
def build_family(seed: PortableQuery, target: TargetSystem) -> FamilyBuildOutcome:
    purity = assess_query_purity(seed.text)
    if not purity.safe:
        return reject(purity.reason)

    parsed = parse_query(seed.text)
    ir = analyze_pattern_ir(parsed)
    capabilities = target_capabilities(target)

    certified_generators = []
    for rule in query_symmetry_registry.core_rules(capabilities):
        sites = rule.find_sites(ir)
        for site in sites:
            generator_certificate = rule.certify_generator(
                ir, site, capabilities
            )
            if not generator_certificate.accepted:
                record_reject(generator_certificate.reason_code)
                continue
            certified_generators.extend(
                rule.generators(ir, site, generator_certificate)
            )

    bundles = compatible_generator_bundles(
        certified_generators,
        max_generators=config.max_generators_per_family,
        seed=seed.seed,
    )
    for bundle in bundles:
        certificate = certify_composition(ir, bundle, capabilities)
        if not certificate.accepted:
            record_reject(certificate.reason_code)
            continue

        try:
            actions = orbit_enumerator.enumerate(
                generators=bundle,
                max_members=config.max_family_members,
                seed=seed.seed,
                revalidate_each_prefix=True,
            )
        except CompositionPreconditionError as error:
            record_reject(error.reason_code)
            continue

        members = deduplicate_presentations(
            renderer.render(ir, action) for action in actions
        )
        if len(members) < config.min_family_members:
            record_diagnostic(
                reason="DIAGNOSTIC_FAMILY_CARDINALITY",
                members=members,
                certificate=certificate,
            )
            continue
        if any(analyze_pattern_ir(parse_query(q)).semantic_hash != ir.semantic_hash
               for q in members):
            raise EvidenceIntegrityError("rendered family escaped semantic orbit")
        return accept(CertifiedQueryFamily(...))

    return reject("NO_CERTIFIED_QUERY_SYMMETRY")
```

生成预算必须区分：

- GDsmith seed 数；
- 可解析 seed 数；
- 存在可认证对称位点的 seed 数；
- 存在可联合认证生成元组合的 seed 数；
- 仅形成 2–3 成员诊断候选的 seed 数；
- 构成至少 4 成员正式查询族的 seed 数；
- 动态结果一致的查询族数；
- 进入 runtime screen 的查询族数。

这些分母不能混用。

## 7. 动态结果门禁

新增：

```text
src/graph_sym_perf/workload/family_acceptance.py
src/graph_sym_perf/equivalence/family_checker.py
```

执行顺序：

1. 记录加载完成后的图快照指纹；
2. 对每个成员执行普通只读查询；
3. 拒绝异常、超时、截断和不稳定结果；
4. 按有序序列或无序多重集比较完整类型化结果；
5. 再次检查图快照指纹，确认所有成员执行在同一状态上；
6. 保存每个成员的结果摘要哈希和统一结果等价证据。

若静态证书认为成员等价，但目标 GDBMS 返回不同结果：

- 不进入性能比较；
- 形成独立 `CorrectnessFinding`；
- 不能静默删掉异常成员后继续把剩余成员当作完整轨道。

## 8. 查询族级运行时间测量

新增：

```text
src/graph_sym_perf/runtime/family_measurement.py
src/graph_sym_perf/runtime/family_oracle.py
src/graph_sym_perf/runtime/family_models.py
```

### 8.1 平衡执行日程

对 \(m\) 个成员和 \(b\) 个测量 block：

- 每个 block 执行所有成员一次；
- 使用种子化循环拉丁方或等价平衡排列，使每个成员出现在各执行位置的次数尽量相同；
- block 顺序随机化；
- warmup 与正式样本分离；
- 任一成员 timeout 时，该 block 结束并形成有下界的无结论证据；
- 正式筛选只使用 ordinary execute，不使用 `PROFILE` elapsed。

```python
@dataclass(frozen=True)
class FamilyBlockSample:
    block_index: int
    order: tuple[str, ...]
    elapsed_ms: Mapping[str, float]
    timeout_members: frozenset[str]
```

### 8.2 族内离群统计

先对每个成员计算 block 中位数：

\[
\tilde T_i=\operatorname{median}_b T_{b,i}.
\]

定义查询族离散度：

\[
D(\mathcal F)=
\frac{\max_i \tilde T_i}{\min_i \tilde T_i}.
\]

只用 \(D\) 不够，因为所有成员可能形成多个正常性能簇。推荐增加“其余成员成簇”条件。对候选慢成员 \(o\)：

\[
D_{-o}=
\frac{\max_{i\ne o}\tilde T_i}
{\min_{i\ne o}\tilde T_i}.
\]

初筛条件：

1. \(D(\mathcal F)\ge \theta_\text{screen}\)；
2. 存在成员 \(o\) 使 \(D_{-o}\le \theta_\text{cluster}\)；
3. \(o\) 相对其余成员稳健中心的绝对差大于 A/A 噪声 p99；
4. 多数 block 中 \(o\) 保持同方向更慢；
5. 所有成员结果完整等价。

这使预言表达为“同一对称族中一个表示异常慢，而其他表示互相一致”，比任意选择一对查询更有定位力。

对于只有 2–3 个成员的候选，保留现有 pair oracle 或小族测量作为开发诊断与历史兼容层，
统一报告为 `DIAGNOSTIC_ONLY`；它们不进入正式查询族级预言，也不进入核心产率、性能异常
发现数或论文主评价。

### 8.3 独立复验

候选必须在两个 fresh session 中复验：

- 每次重新连接；
- 保持同一图快照；
- 重新随机化平衡日程；
- 两次 session 的异常成员相同；
- 合并后置信区间下界、绝对差和方向一致性均过门；
- A/A 与 member/member 自身对照均未超噪声；
- 计划信息只在通过后采集。

最终证据保存完整查询族，不只保存最快和最慢成员。

## 9. 约简

现有 pair reducer 不能分别修改两条查询。新增 `FamilyReducer`：

1. 只约简种子查询的 `PatternIR` 或配套图状态；
2. 每次约简后重新运行规则适用检查；
3. 从约简后的种子重新生成整个查询族；
4. 重新生成证书；
5. 重新执行结果门禁和族级 runtime oracle；
6. 只有异常成员身份和离群关系均保留时接受约简。

图约简允许删除与规则 witness 无关的节点、边和属性，但不要求约简图具有对称性。需要保持的是：

- 所有查询族成员仍在同一个约简图上执行；
- 规则证书仍成立；
- 结果仍完整一致；
- 同一成员仍是稳定性能离群点。

## 10. 现有代码迁移

### 10.1 复用

| 现有模块 | 处理 |
|---|---|
| `generation/gdsmith.py`、`gdsmith/runner.py` | 保留，继续生成图与 seed query |
| `cypher/ast.py`、`cypher/parser.py` | 扩展为 `PatternIR` 输入，第一阶段保持有限支持域 |
| `cypher/proof.py`、`cypher/relational.py` | 复用关系代数等价检查，增加查询族成员的统一语义哈希 |
| `transforms/safety_points.py` | 提取通用证书字段，新增查询族级证书 |
| `equivalence/*` | 扩展为 family checker |
| `runtime/measurement.py` | 保留 pair 兼容层，抽取共享计时原语 |
| `runtime/noise.py`、`runtime/statistics.py` | 复用噪声与 bootstrap 统计 |
| `validation/*` | 从 pair validation 扩展为 family validation |
| `experiment/worker.py` | 替换正式路径的 pair builder/screener |
| `reducer/*` | 保留 ddmin 与依赖分析，新增 family-preserving reducer |

### 10.2 从正式路径移除

- `bool_redundancy` 正式规则；
- `RuleRegistry` 中所有 BOOL CORE；
- `enabled_families: [bool_redundancy, ...]`；
- 以谓词顺序、标签顺序和 alpha renaming 计入核心产率；
- 任何 `symmetric_state`、`involutive_double` 或双拷贝图构造；
- `Q' / Q` 单方向倍率作为唯一候选条件；
- 以计划差异提升候选。

历史模块可先移动到 `legacy` 或只由 historical replay 入口引用；在 import contract 确认无正式入口依赖后再删除。

### 10.3 配置草案

```yaml
query_symmetry:
  min_family_members: 4
  max_family_members: 12
  allow_certified_generator_composition: true
  max_generators_per_family: 4
  below_minimum_policy: diagnostic_only
  orbit_mode: bounded_deterministic
  core_rules:
    - undirected_pattern_linearization
    - match_pattern_component_permutation
  experimental_rules:
    - bounded_undirected_path_endpoint
    - mandatory_match_dependency_permutation
  controls:
    - alpha_renaming
    - directed_notation

runtime:
  family_screen:
    warmups_per_member: 2
    blocks: 7
    dispersion_ratio: 2.0
    residual_cluster_ratio: 1.25
    schedule: balanced_random_blocks
  family_validation:
    sessions: 2
    blocks_per_session: 20
    min_outlier_ratio: 3.0
    min_ci_lower_ratio: 2.0
    min_absolute_delta: aa_noise_p99
    require_same_outlier_member: true
```

具体阈值必须通过 A/A 校准和先导实验确定，不能在论文中表述为领域通用标准。

## 11. 制品契约

新增最小制品：

```text
families/<family-id>/
├── family.json
├── applicability-certificate.json
├── members/
│   ├── <member-id>.cypher
│   └── ...
├── equivalence.json
├── discovery-runtime.json
└── validation/
    ├── session-1.json
    └── session-2.json
```

`family.json` 至少包含：

- 图快照哈希；
- seed query 与生成器版本；
- `PatternIR` schema 版本和语义哈希；
- 全部规则及其版本、生成元、单生成元证书摘要、联合证书、动作词和轨道完整性；
- 每个成员的文本哈希与 presentation hash；
- 结果等价状态；
- 运行日程、噪声门和异常成员；
- 代码、DBMS、驱动和配置版本。

任何无法从制品重新验证证书和查询族身份的候选都不能进入 issue promotion。

## 12. 测试方案

### 12.1 单元测试

- `PatternIR` 对允许的表示变化产生相同 `semantic_hash`；
- 方向、标签、类型、属性、投影或作用域变化产生不同哈希；
- 对称动作的恒等元、逆元和组合满足预期；
- 两个分别安全但联合前提冲突的生成元组合必须被拒绝；
- 联合证书覆盖动作词的每个前缀，篡改生成元顺序或位点后验证失败；
- renderer 生成成员后重新解析，语义哈希不变；
- 伪造证书、错误 capability hash 和错误 site 被拒绝；
- 轨道枚举确定、无重复、受预算限制；
- 平衡日程中每个成员的位置分布满足界限；
- 族级 oracle 能区分单离群、双簇、整体噪声和 timeout。

### 12.2 性质测试

在小规模随机 `PatternIR` 上验证：

\[
\forall \gamma\in O_{\mathcal R}(Q),\quad
\operatorname{semanticHash}(\gamma Q)
=
\operatorname{semanticHash}(Q).
\]

并对自环、平行边、重复关系变量、多标签、NULL 属性、多组件模式和路径边界做定向生成。

### 12.3 真实 GDBMS 集成测试

最小图必须刻意包含不对称数据分布，以证明方法不依赖图状态对称：

- 一个端点高选择性、另一个端点高基数；
- 入度/出度严重偏斜；
- 平行边和自环；
- 稀有标签与常见标签；
- 有索引和无索引两组。

在该图上验证查询族结果一致，并检查不同文本确实到达目标 DBMS。

### 12.4 回归与负对照

- 保留历史性能 issue 作为 known replay，不把它们计入新方法发现产率；
- alpha renaming 和 directed notation 作为控制族；
- predicate order 只作与普通等价重写的消融对照；
- 构造一个结果不等价的路径反转，确认静态或动态门禁拒绝；
- 构造一个 A/A 高噪声成员，确认无法 promotion。

## 13. 分阶段实现

### Phase 0：语义冻结

- 写 ADR：任意固定 \(G\)、查询表示对称、图状态不变；
- 落实已锁定的两个 CORE 规则及其正式支持域；
- 定义 `PatternIR`、证书和制品 schema；
- 添加“正式路径不得导入 bool/symmetric_state”的 contract test。

验收：无代码路径歧义，所有术语在 README、配置和方法文档一致。

### Phase 1：PatternIR 与单规则端到端

- 实现 `PatternIR`；
- 实现 `undirected_pattern_linearization`；
- 生成至少 2 个成员以验证动作、语义哈希和结果门禁；
- 接入完整结果门禁；
- 复用 pair runtime 先跑两成员兼容实验，但产物必须标记为 `DIAGNOSTIC_ONLY`。

验收：在非对称图状态上稳定生成并验证同结果查询；本阶段的 2–3 成员产物不得进入正式
查询族统计。

### Phase 2：查询族与证书

- 实现轨道枚举和 bounded sample；
- 实现 `CertifiedQueryFamily`；
- 实现生成元兼容束枚举、联合证书和动作前缀重认证；
- 实现 `match_pattern_component_permutation`；
- 持久化完整证书和成员动作。

验收：单规则或多规则生成元可经联合认证形成至少 4 成员的正式查询族；分别安全但组合
不安全的生成元被拒绝；查询族可重放，伪造证书无法通过；少于 4 成员时 fail closed 到诊断
路径。

### Phase 3：族级 runtime oracle

- 实现平衡 block 日程；
- 实现离散度、残余成簇和异常成员判定；
- 接入两个 fresh session；
- 计划只在复验后采集。

验收：模拟器和真实 DBMS 噪声注入测试均通过。

### Phase 4：正式 worker 迁移

- 正式配置删除 BOOL；
- `worker.py` 改为 seed → family → equivalence → family runtime；
- 调度单位改为 accepted families；
- 保留明确的 seed、family、member 分母。

验收：单 GDBMS `1×10` gate 完整运行且无 BOOL/symmetric-state artifact。

### Phase 5：family-preserving reducer 与 issue

- 约简 seed IR 和图；
- 每次重新生成完整族与证书；
- issue exporter 保存完整族和异常成员；
- known replay 与新发现分池。

验收：约简后仍是同一规则族，异常成员身份和 runtime 关系复现。

### Phase 6：评价

- 与当前 pair-SYM、GRev 和控制族比较；
- 报告 certified-family yield、平均族规模、动态接受率、screen/validation yield、独立 bug family；
- 消融证书、族级预言和 pair-only oracle；
- 不用 issue 数量替代语义可信度评价。

## 14. 可行性评估

| 子问题 | 可行性 | 依据 | 主要风险 |
|---|---|---|---|
| 同一任意 \(G\) 上无向端点表示对称 | 高 | 当前 AST/RA proof 已有基础 | 方言解析差异 |
| 同 MATCH pattern 分量族 | 中高 | 当前 parser 支持 pattern list | 关系唯一性和复杂 pattern |
| 规则适用证书 | 高 | 已有 `SafetyCertificate` 与 promotion gate | 现有证书仍偏 pair |
| 4–12 成员查询族生成 | 中高 | 组合规模可有界枚举 | 有效族产率未知 |
| 族级平衡 runtime oracle | 高 | 可复用 pair harness、noise、bootstrap | 正式实验成本增加 |
| mandatory MATCH 排列 | 中 | 依赖分析已有 reducer 基础 | Cypher 作用域和 WHERE 附着 |
| 可变长度路径端点族 | 中低 | 有有限路径 proof 雏形 | 多重路径、路径对象和跨方言语义 |
| 直接复用 GRev 源码 | 低到中 | 有公开工程和 ASG/RWC 实现 | 许可证、工程耦合和接口稳定性 |
| 把 GRev 作为外部基线 | 高 | 公开论文与复现实验工程 | 环境版本较旧、运行成本 |

总体判断：**核心原型可行，论文创新性有条件可行。**

工程风险不是最大问题；最大风险是与 GRev 的新颖性重叠。只有同时做到以下三点，贡献才有可能成立：

1. 查询族来自显式图模式表示对称群，而不是一般 EQR；
2. 每个具体族都有可审计、可拒绝、可重放的适用证书；
3. 性能候选由查询族内部离群关系和独立复验定义，而不是任意查询对倍率。

如果最后仍然只实现“端点反转 + 两条查询计时”，该工作会被审稿人视为 GRev/AMOEBA 的窄规则复现，贡献不足。

## 15. 已锁定决策与待讨论项

建议按以下顺序逐项讨论：

1. **正式测试单位（已锁定）**：正式查询族至少 4 个成员；2–3 成员只保留开发诊断和历史
   兼容层，不进入核心产率或论文查询族级预言评价。
2. **生成元组合策略（已锁定）**：允许单规则或多规则的已认证生成元组合形成至少 4 成员
   轨道，但必须通过联合证书和动作前缀验证。
3. **第一批 CORE（已锁定）**：仅包含 `undirected_pattern_linearization` 和
   `match_pattern_component_permutation`；前者承担主要图原生方法证据，后者严格限定为
   同一 `MATCH` 内 pattern list 的结构化族扩展。
4. **新颖性边界**：是否接受“规则本身未必新，创新在结构化族、证书和族级预言”这一收紧表述。
5. **GRev 角色**：推荐只作外部基线与覆盖对照，不直接嵌入正式主线。
6. **正式统计单位**：从 accepted query pairs 改为 accepted certified query families；member 数作为第二层统计。
7. **历史 BOOL 路线**：推荐从正式代码入口和论文贡献中移除，但保留 historical replay 与旧 issue 证据。
8. **计划信息**：推荐继续保持解释证据，不参与候选生成、调度和 promotion。

前三项已经锁定；其余五项锁定前，不建议开始大规模改代码。

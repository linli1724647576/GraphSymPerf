# GraphSymPerf：通过上下文可证明的安全实例化测试图数据库系统

> English title: **GraphSymPerf: Contextually Certified Safe Instantiation of Equivalent Query Rewrites for Testing Graph Database Systems**
>
> 文档状态：方法重构后的中文初稿。目标为 CCF-A 类软件工程或数据库会议/期刊，具体 venue、页数和匿名要求待确定。
>
> 诚信说明：`[MATERIAL GAP]` 表示方法、制品或外部事实尚待补齐；`[RESULT GAP]` 表示实验尚未完成。带缺口的内容不能直接用于投稿结论。

## 论文配置与核心论点

**论文类型。** 图数据库管理系统（Graph Database Management System, GDBMS）测试方法与实证评估。

**唯一核心创新。** 上下文可证明的安全实例化（contextually certified safe instantiation）：给定一般查询生成器产生的有效查询和一组条件等价规则，显式识别规则能够被安全实例化的位置，并为每个候选位置生成忠实、可追溯、可重放的重写适用性解释。

**非核心内容。** 基础查询生成器、候选归约、issue 去重和开发者报告链是实现或实验支持，不作为主要方法创新。

**三层贡献。**

1. 形式化条件等价规则在一般生成查询中的上下文安全实例化问题。
2. 设计 GraphSymPerf，以属性化查询上下文图、声明式证明义务、三值判定、重写适用性解释和纯静态覆盖实现该问题。
3. 在多个 GDBMS 上评估安全性、组件作用以及逻辑与性能缺陷检测能力。

## 摘要

等价查询重写能够为图数据库测试构造无需人工结果标注的查询对，但一条重写规则通常只在特定的类型、空值、作用域、图模式和下游观察条件下保持等价。在一般查询生成器产生的复杂查询中，这些条件可能跨越多个子句和图模式；若规则适用性被隐含在转换代码或生成过程里，测试器难以解释为什么某个位点安全，也难以系统探索规则的合法实例空间。本文提出 GraphSymPerf，将等价查询测试中的规则应用形式化为上下文可证明的安全实例化问题。GraphSymPerf 首先把查询表示为属性化查询上下文图（Attributed Query Context Graph, AQCG），统一编码语法结构、符号绑定、语义流和图拓扑。每条条件等价规则声明结构候选模式、证明义务、规则级等价性定理和方向化重写构造器。对于每个结构候选，GraphSymPerf 从 AQCG 提取由证明义务驱动的上下文切片，并在开放世界假设下给出 `SAFE`、`UNSAFE` 或 `UNKNOWN` 判定，同时生成可机器复核的重写适用性解释（Rewrite Applicability Explanation, RAE）。框架随后只在 `SAFE` 实例中进行证明义务驱动的静态覆盖，并分别使用严格结果不一致 Oracle 和稳定配对性能不对称 Oracle 检测逻辑与性能缺陷。项目当前累计公开报告29个 issue，其中25个被分类为性能问题、4个被分类为逻辑问题；按当前维护状态，21个为开发者确认或修复的问题，但该状态仍需在投稿前逐项复核。`[RESULT GAP: 完成 CORE-v1、统一安全性验证、组件消融和冻结后的跨系统实验后，补充正式结果与效应量。]`

## 1 引言

图数据库通过节点、关系和路径上的模式匹配支持高度关联的数据查询。复杂的变量作用域、三值逻辑、可变长度路径、聚合和图模式交互也使 GDBMS 容易出现错误结果和异常性能。由于任意生成查询通常没有已知正确结果，图数据库测试长期依赖差分测试、查询分区、蜕变关系和等价查询重写来构造测试 Oracle \cite{hua2023gdsmith,kamm2023gdbmeter,zhuang2023gamera,mang2024grev}。

等价查询重写（Equivalent Query Rewriting, EQR）为这一问题提供了直接思路：从查询 \(Q\) 构造语义等价的 \(Q'\)，再比较两者的可观察行为。GRev 使用抽象语法图（Abstract Syntax Graph, ASG）编码图模式语义，并通过路径覆盖生成等价查询 \cite{mang2024grev}。Dinkel 在生成过程中维护查询上下文和图 Schema 状态，并应用子句级与表达式级细粒度变换 \cite{wust2026dinkel}。这些工作说明了图结构表示、有效查询生成和细粒度变换的价值，也意味着本文不能把“使用上下文”或“双重否定等局部规则”本身当作新颖性来源。

本文关注一个更窄但尚需被显式处理的问题：**当条件等价规则面对一般生成查询时，规则究竟可以在什么位置被安全实例化，判定依据能否被检查和解释？** 例如，将一个已绑定节点作为无约束节点模式加入 `OPTIONAL MATCH`，只有在该符号在当前作用域可见、已在上游绑定、类型为节点且不可能为空时，才能作为恒等模式。仅查看待修改的 AST 子树不足以获得这些事实；以特定 GDBMS 名称或已知 issue 查询形状编码 guard 又会削弱通用性。

GraphSymPerf 将规则适用性提升为一等研究对象。框架使用 AST 与属性化查询上下文图组成的统一中间表示，分离结构候选发现和语义安全判定。每条规则通过受限声明式语言给出证明义务；统一求值器基于带来源的 AQCG 事实产生三值结果。规则级条件等价定理证明“前提成立则重写等价”，实例级 RAE 则解释“这些前提在当前位点为何成立或为何无法成立”。证明义务同时导出静态覆盖因素，使测试器能够系统探索合法语义上下文，而不使用执行计划、运行时间或已知 issue 反馈指导变换选择。

本文的贡献如下。

1. **问题与形式化。** 我们形式化上下文可证明的安全实例化，将条件等价规则表示为结构候选模式、声明式证明义务、规则级等价性定理和方向化重写构造器，并区分 `SAFE`、`UNSAFE` 和 `UNKNOWN`。
2. **方法与系统。** 我们设计 GraphSymPerf，以 AQCG、规则相关上下文切片、可重放 RAE 和证明义务驱动的静态覆盖，在一般生成查询中实例化表达式、谓词、作用域和图模式级规则。
3. **实证评估。** 我们设计安全性验证、组件消融和跨 GDBMS 缺陷检测实验，并使用一个累计 bug 指标报告开发者确认或修复的逻辑与性能问题。`[RESULT GAP: 完成冻结协议下的正式评估。]`

## 2 背景与相关工作

### 2.1 图数据库测试 Oracle

GDsmith 通过图和 Cypher 查询生成结合差分测试发现 GDBMS 缺陷 \cite{hua2023gdsmith}。GDBMeter 使用查询分区构造逻辑结果 Oracle \cite{kamm2023gdbmeter}。Gamera 设计图结构感知蜕变关系，以图模式变化验证结果一致性 \cite{zhuang2023gamera}。这些工作主要说明如何生成图查询或构造特定 Oracle。GraphSymPerf 不提出新的基础查询生成器，而把有效基础查询提供者视为可替换组件。

### 2.2 等价查询重写

GRev 将 EQR 引入 GDBMS 测试。其 ASG 把图模式中的节点、关系和约束编码为图结构，Random Walk Covering 从 ASG 中选择覆盖完整模式语义的路径集合，再翻译为等价查询 \cite{mang2024grev}。GraphSymPerf 沿用“图结构适合承载查询语义”的基本观察，但目标不同：AQCG 不以重新覆盖图模式路径为主要生成机制，而是表达规则在具体位点所依赖的绑定、空值、作用域、基数、顺序和下游可观察性事实。

Dinkel 在查询生成过程中维护变量、类型和作用域组成的查询上下文以及标签、属性组成的图 Schema，并对任意生成查询应用子句级和表达式级变换 \cite{wust2026dinkel}。其规则集合已经包含双重否定、布尔中性元素和其他细粒度变换。因此，GraphSymPerf 不把这些局部重写规则重新包装为创新。本文的研究增量在于：将每个具体位点的适用前提外显为封闭谓词词汇上的证明义务，使安全判定及其依据成为可复核工件，并以这些义务定义静态探索空间。

### 2.3 查询等价与性能 Oracle

GraphQE 等工作研究 Cypher 查询等价性的形式化证明 \cite{tang2025graphqe}。GraphSymPerf 不声称证明任意 Cypher/GQL 查询等价，而只对显式分析语言和规则适用域内的条件等价规则给出证明。AMOEBA 使用等价 SQL 查询之间的运行时间差检测关系数据库性能问题 \cite{liu2022amoeba}。GraphSymPerf 将类似思想用于 GDBMS，但要求性能 Oracle 通过严格语义门，并使用配对、稳定性和独立复现条件；执行计划只作为事后解释材料。

## 3 方法

### 3.1 问题定义

GraphSymPerf 的输入为：

- 一个有效只读查询 \(Q\)；
- 图 Schema \(\Sigma\)；
- 方言语义描述 \(D\)；
- 条件等价规则集合 \(\mathcal R\)。

基础查询可以来自 GDsmith、Dinkel 式生成器、已有测试语料或其他有效查询提供者。查询生成不是本文贡献。

定义查询观察：

\[
Obs_D(G,Q)=
\langle
columns,\ typed\ values,\ multiplicities,\ order
\rangle .
\]

严格结果等价 \(\equiv_{\mathrm{strict}}\) 要求：

- 返回列及其顺序一致；
- 标量和值类型一致，例如整数 `1` 与浮点数 `1.0` 不自动合并；
- `NULL`、列表、路径和复合值的表示一致；
- 每一行的出现次数一致；
- 列表和路径的内部顺序一致；
- 只有当查询语言建立行顺序契约时才逐行比较，否则按多重集比较。

逻辑不一致 Oracle 的对象是两个均成功执行但结果不满足严格等价的查询。性能 Oracle 只有在两个查询均成功执行且结果严格相等后才启用。非对称错误、崩溃和无法获得结果的超时作为诊断事件记录，不进入两个正式 Oracle。

GraphSymPerf 的统一分析语言记为 \(\mathcal L_A\)。它定义 AQCG 能够解析和分析的只读构造。每条规则 \(r\) 另有显式适用域 \(D_r\subseteq\mathcal L_A\)。本文只在 \(\mathcal L_A\) 和对应 \(D_r\) 内主张可靠性，不声称覆盖完整 Cypher 或 GQL。

### 3.2 属性化查询上下文图

GraphSymPerf 将 AST 与属性化查询上下文图结合，定义：

\[
AQCG(Q)=
(N_C\cup N_E\cup N_S\cup N_P,\,
E_H\cup E_B\cup E_F\cup E_G,\,
\alpha).
\]

四类节点分别表示：

- \(N_C\)：`MATCH`、`WHERE`、`WITH`、`RETURN`、`ORDER BY`、`LIMIT` 和 `UNION` 分支等子句；
- \(N_E\)：布尔、标量、聚合和投影表达式；
- \(N_S\)：符号定义与符号引用，定义节点和引用节点分离；
- \(N_P\)：节点、关系、路径和模式组合。

四类边分别表示：

- \(E_H\)：AST 层级与包含关系；
- \(E_B\)：`DEFINES`、`USES`、`RESOLVES_TO` 和 `SHADOWS` 等绑定与作用域关系；
- \(E_F\)：过滤、投影、聚合、排序、限制和下游观察等语义流；
- \(E_G\)：端点、共享变量、路径组合、别名和约束等图拓扑关系。

AQCG 维护带来源的事实：

\[
f=
\langle
predicate,\ arguments,\ value,\ provenance,\ derivation
\rangle.
\]

事实来自查询语法、Schema、方言语义声明或其他事实的保守推导。缺失事实不解释为假，而是产生 `UNKNOWN`。系统运行时间、执行计划、异常结果和 issue 标识不进入 AQCG。

最小事实词汇由 CORE 规则的证明义务驱动，包括：

```text
Visible / BoundBefore / ResolvesTo / Shadowed
TypeOf / Nullable / Pure / Deterministic / Total
OrdinaryMatch / OptionalBoundary / SameMatchBoundary
SharedVariables / PatternImplies / ConstraintEquivalent
Grouped / AggregateScope / CandidateKey / RowUnique
OrderRequired / CardinalityOnly
Observable / PathObservable / RelationshipObservable
ReadOnlyBranch / UnionCompatible / DialectSupports
```

对于候选位点 \(s\) 和规则 \(r\)，上下文不是固定文本窗口或整个 AQCG，而是证明义务所需语义依赖的并集：

\[
Ctx_r(Q,s)=
\bigcup_{o\in O_r}
Dependencies(AQCG(Q),s,o).
\]

同一位点对不同规则可以具有不同上下文切片。若依赖路径经过当前无法分析的构造，相应义务产生 `UNKNOWN`。

`[MATERIAL GAP: 实现统一 AQCG 构建器、稳定节点标识、事实来源和推导 DAG。]`

### 3.3 条件等价规则

等价关系在语义上是对称的，但语法实例化具有方向。对方向 \(d\) 的规则定义为：

\[
r^d=
\langle
M_r^d,\,
O_r^d,\,
T_r,\,
W_r^d
\rangle ,
\]

其中：

- \(M_r^d\) 是结构候选模式；
- \(O_r^d\) 是声明式证明义务；
- \(T_r\) 是正反方向共享的条件等价性定理；
- \(W_r^d\) 是方向化重写构造器。

候选匹配器只能做结构匹配和元变量绑定，不得执行隐藏的语义 guard。重写构造器只能构造目标查询，不得绕过统一求值器增加安全判断。所有 CORE 规则使用受限的声明式证明义务语言，由原子谓词以及 `AllOf`、`AnyOf` 和 `Not` 组成。

规则级正确性契约为：

\[
M_r^d(Q,s,\theta)
\land
Eval(O_r^d,Ctx_r(Q,s),\theta)=\texttt{SAFE}
\Rightarrow
\llbracket Q\rrbracket_G
=
\llbracket W_r^d(Q,s,\theta)\rrbracket_G.
\]

语义义务在已支持分析域内被明确否定时产生 `UNSAFE`。缺少事实、未支持的语言构造、超出规则证明域或方言能力不足时产生带原因的 `UNKNOWN`。因此，`UNSAFE` 不用于表示“框架尚未实现”。

CORE-v1 的8条优先规则如下。最终只有完成规则级证明、实现、有界验证和前提消融闭环的规则进入 CORE；其他规则降为 `EXPERIMENTAL`。

| 层级 | 优先规则 | 主要上下文事实 |
|---|---|---|
| 表达式 | `DoubleNegationPreservation` | 三值布尔类型与表达式上下文 |
| 图模式 | `PatternConjunctionPermutation` | 模式边界、共享变量和正模式合取 |
| 模式—谓词 | `ConstraintExternalization` | 标签/类型约束与谓词等价 |
| 聚合—空值 | `NonNullCountEquivalence` | 非空性、聚合作用域 |
| 下游观察 | `OrderEliminationUnderCardinalityObservation` | 顺序契约和基数观察 |
| 图拓扑 | `ZeroHopPathCollapse` | 端点统一、约束合并和路径可观察性 |
| 作用域—模式 | `BoundNodeReinforcement` | 上游绑定、节点类型、空值和遮蔽 |
| 查询组合 | `ReadOnlyUnionPermutation` | 分支只读性、输出兼容和顺序契约 |

第二优先级候选包括 `PredicateIdempotence`、`BooleanNeutralEmbedding`、`EntailedConstraintReinforcement`、`StaticPropertyAccessEquivalence`、`ConditionalCountExternalization` 和 `KeyImpliedDistinctElimination`。

规则来自真实 issue 的启发并不自动构成过拟合。CORE 规则必须满足参数化、系统身份无关、全称条件等价、独立合成验证和跨方言语义可实例化要求。issue 中的固定标签、属性、常量、数据库名称和编号不能进入规则定义。

`ReadOnlyUnionPermutation` 是优先集合中证明风险最高的规则。它只有在方言语义描述明确给出 `UNION` 的重复消除、类型兼容和值相等关系时才能产生 `SAFE`。跨类型数值（如整数 `1` 与浮点数 `1.0`）可以作为压力测试维度，但不能在语义未定义时被默认为等价；事实不足时必须返回 `UNKNOWN`。若最终只能建立代数一致性属性而无法建立方言规范下的条件等价定理，该规则应降为 `EXPERIMENTAL`。

### 3.4 上下文可证明的安全实例化

对于规则 \(r\)，GraphSymPerf 首先枚举结构候选：

\[
Cand_r(Q)=
\{(s,\theta)\mid AQCG(Q)\models M_r(s,\theta)\}.
\]

候选匹配器在 \(M_r\) 定义的结构空间内必须相对完备：允许过生成候选，再由证明义务拒绝；不允许为了提高 `SAFE` 比例而静默遗漏结构候选。每个候选均获得 `SAFE`、`UNSAFE` 或 `UNKNOWN` 判定。

安全位点集合为：

\[
Safe_r(Q)=
\{(s,\theta)\in Cand_r(Q)
\mid Eval(O_r,Ctx_r(Q,s),\theta)=\texttt{SAFE}\}.
\]

规则级定理解决“前提成立时重写是否等价”，实例级 RAE 解决“当前位点是否满足这些前提”。RAE 定义为：

\[
RAE=
\langle
rule,\ direction,\ site,\ bindings,\ theorem,\ obligationTree,\ factTrace,\ decision
\rangle.
\]

`SAFE` 解释展示全部定理前提及其事实来源；`UNSAFE` 解释展示被明确否定的语义义务；`UNKNOWN` 解释展示缺失事实、超出证明域或不支持的方言能力。自然语言说明由结构化记录确定性生成，结构化记录是唯一事实来源。

#### 贯穿案例：BoundNodeReinforcement

考虑方向化规则：

\[
\texttt{OPTIONAL MATCH }P
\quad\Rightarrow\quad
\texttt{OPTIONAL MATCH }P,(x).
\]

新增的 `(x)` 是无标签、无属性、无路径变量和无其他约束的独立节点原子。候选匹配器绑定 `OPTIONAL MATCH` 子句 \(c\)、模式 \(P\)、候选符号 \(x\) 和插入位置，但不在匹配阶段判断 \(x\) 是否安全。

语义义务为：

```text
VisibleAt(x, c)
BoundBefore(x, c)
TypeOf(x) = NODE
NullableAt(x, c) = FALSE
NoRebinding(x, c)
NoShadowing(x, c)
```

证明域和能力条件为：

```text
SupportedOptionalPattern(P)
OrdinaryReadOnlySemantics(c)
DialectSupports(UnconstrainedBoundNodePattern)
NoUnsupportedPatternOperator(P)
```

对每个进入该子句的绑定行 \(\mu\)，若：

\[
\mu(x)=v,\qquad v\in V_G,\qquad v\neq NULL,
\]

则无约束节点模式 `(x)` 只产生恒等匹配：

\[
Eval((x),G,\mu)=\{\mu\}.
\]

因此：

\[
Eval(P,(x),G,\mu)=Eval(P,G,\mu),
\]

从而保持 `OPTIONAL MATCH` 的匹配行、空值扩展和重复次数。反向删除规则独立声明候选模式和证明义务，但共享同一个条件等价定理。LadybugDB #697 可作为该通用规则的一个真实实例，而不是规则定义本身。

CORE-v1 的正式测试单元是单规则、单位点、单步重写：

\[
(Q,Q')=
\left(Q,W_r^d(Q,s,\theta)\right).
\]

多步组合只有在每一步都对更新后的查询重新构建上下文并生成新 RAE 后才保持可认证；其初期仅作为 `COMPOSED` 扩展，不进入核心结果。

`[MATERIAL GAP: 实现证明义务 DSL、统一三值求值器、RAE 序列化与确定性自然语言渲染。]`

### 3.5 证明义务驱动的静态探索

GraphSymPerf 不使用运行时间、执行计划、异常结果或 issue 编号指导重写选择。对每个 `SAFE` 实例，框架从定理前提和 AQCG 上下文提取覆盖向量：

\[
V(r,s)=
\langle
rule,\ direction,\ level,\ context,\ witness,\ boundary
\rangle.
\]

其中 `witness` 表示同一证明义务成立的不同证据来源。例如，`NonNull(e)` 可以由非空字面量、模式绑定、Schema 约束或前置谓词证明。这些实例满足同一个条件等价定理，但覆盖不同的上下文推导路径。

探索策略依次追求：

1. 每个合法因素取值至少被覆盖一次；
2. 关键因素对至少被覆盖一次；
3. 在固定预算下优先填补尚未覆盖的语义单元；
4. 不追求全部笛卡尔积。

`UNSAFE` 和 `UNKNOWN` 候选不作为等价查询执行，但用于 RQ1 的安全分析验证和 guard 消融。方向是覆盖因素，但正反方向生成的相同无序查询对不重复增加异常或 bug 数量。

`[MATERIAL GAP: 定义每条 CORE 规则的因素域、证据来源类别和确定性单因素/成对调度算法。]`

### 3.6 逻辑与性能 Oracle

规则适用性与查询对执行资格分离。一个重写实例可以在语言语义上为 `SAFE`，但因查询包含不可控非确定性而不适合跨执行比较。执行前还需要：

```text
ReadOnly(Q)
StableDatabaseSnapshot
NoUncontrolledNondeterminism
NoExternalSideEffects
SameParametersAndEnvironment
```

只有当某条重写改变表达式的求值次数或依赖时，`Pure/Deterministic` 才作为该规则的语义证明义务；否则它属于查询对的 Oracle 执行资格条件。

逻辑 Oracle 定义为：

\[
O_L(Q,Q')=1
\iff
\begin{cases}
RAE(Q,Q')=\texttt{SAFE},\\
Exec(Q)=Success(R),\\
Exec(Q')=Success(R'),\\
R\not\equiv_{\mathrm{strict}}R'.
\end{cases}
\]

性能 Oracle 先要求严格结果相等，再采用随机化的 `AB/BA` 配对执行。对第 \(i\) 个独立执行块：

\[
d_i=\log\frac{t_i(Q')}{t_i(Q)}.
\]

性能异常需要同时满足：

- 相对效应超过预先冻结的 \(\tau\)；
- 绝对时间差超过预先冻结的 \(\delta\)；
- 多数独立执行块中慢侧方向一致；
- 配对差异的不确定性排除无差异区域；
- 独立会话或独立批次复现成功。

具体阈值、预热次数、执行次数、稳定比例和置信水平由不含29个已有 issue 的噪声 pilot 决定，并在正式测试前冻结。执行计划和性能计数器仅用于异常后的解释，不参与发现判定。

自动判定产生的是性能异常（performance anomaly）。只有被开发者 `confirmed` 或 `fixed` 的唯一问题才累计为性能 bug。

`[RESULT GAP: 完成独立噪声 pilot，冻结 \(\tau,\delta\)、重复次数和置信区间方法。]`

## 4 实现状态与完成标准

> 本节是初稿内部状态说明，投稿版应在实现完成后改写为可复现实现细节，不能原样保留。

当前代码已经提供以下基础：

- 小型、保守的 Cypher AST 和解析器，明确限定“可证明片段”；
- 部分图模式与谓词向关系代数表示的降级和规范化；
- `SemanticProof` 记录定理、片段、前提、不变量及重写前后规范形式；
- 多条对称变换在证明失败或解析失败时保守跳过；
- 完整结果比较和配对运行时验证的既有基础设施。

当前代码尚不能支持本文完整方法主张：

- `safety_points.py` 仍通过字符串分割 `WHERE` 和 `AND` 枚举位置，作用域为空且空值性固定为 `unknown`；
- `SafetyCertificate` 在转换层和验证层存在重复定义；
- 尚无统一 AQCG 构建器；
- 尚无封闭谓词词汇上的证明义务 DSL 和三值求值器；
- 尚无以结构化 RAE 为唯一事实源的解释生成；
- 尚无证明义务驱动的单因素与成对覆盖；
- 8条优先规则尚未全部完成证明、实现和验证闭环。

因此，当前实现只能准确描述为：

> A conservative proof-carrying instantiation substrate over a deliberately small Cypher fragment.

不能描述为“已经实现完整 AQCG 和通用安全实例化框架”。

CORE-v1 的不可删减完成标准为：

1. 统一 AQCG 构建器；
2. 结构化证明义务语言；
3. `SAFE/UNSAFE/UNKNOWN` 三值求值器；
4. 结构化且可重放的 RAE；
5. 从证明义务导出的纯静态覆盖；
6. 每条 CORE 规则完成证明、实现、有界验证和前提消融闭环。

最终 CORE 数量可以少于8，但论文必须同步收缩规则广度主张。

## 5 研究问题与评估设计

### RQ1：安全性与可解释性

> **RQ1. How safely and faithfully does GraphSymPerf instantiate conditional equivalence rules in generated queries?**

证据包括：

- 每条 CORE 规则的条件等价证明；
- 有界小模型中全部 `SAFE` 实例的严格结果一致性；
- 对声称必要的语义前提进行消融并搜索反例；
- 对保守充分条件单独标注，不虚构必要性；
- `UNSAFE/UNKNOWN` 边界测试；
- RAE 判定重放一致率、事实来源完整率和隐藏 guard 审计；
- 候选匹配器相对于 \(M_r\) 的结构完备性。

小模型中未发现反例只作为实现与证明检查，不能替代一般性证明。

### RQ2：核心机制的作用

> **RQ2. How do AQCG-based context analysis and proof-obligation-driven static coverage contribute to safe and diverse rewrite instantiation?**

两个核心对比为：

1. AQCG 分析与仅使用局部 AST 信息的保守分析；
2. 证明义务驱动覆盖与 `SAFE` 位点中的均匀随机选择。

在相同基础查询、规则和执行预算下报告：

- 结构候选数；
- `SAFE/UNSAFE/UNKNOWN` 分布；
- 错误接受实例数；
- 可执行查询对数量；
- 单因素和成对语义单元覆盖；
- 规则、方向和上下文分布；
- 异常数量作为次要指标。

局部 AST 基线不能通过删掉必要 guard 来人为制造不安全优势。另设的 guardless 变体只用于 RQ1 反例实验，不进入缺陷报告。

### RQ3：有效性与通用性

> **RQ3. How effectively does GraphSymPerf expose logical inconsistencies and stable performance asymmetries across different GDBMSs, rewrite rules, and semantic contexts?**

报告：

- 各 GDBMS 上的基础查询、结构候选、`SAFE` 实例和成功查询对；
- 严格逻辑不一致和稳定性能异常；
- 规则族、方向、上下文层级和系统分布；
- 唯一公开 issue reports 数量；
- 一个累计的开发者 `confirmed/fixed` bug 数量。

主文不把开发期和冻结后 bug 拆成两个累计数字，但在实验协议和有效性威胁中披露29个 issue 对规则族抽象的作用。bug 去重以触发结构、症状、版本、平台和修复范围为依据；方向相反或查询文本不同不自动构成新 bug。

### 5.1 规则验证

每条 CORE 规则执行四层验证：

1. 在 \(D_r\) 内给出规则级条件等价证明；
2. 枚举小图、`NULL`、重复行、作用域和上下文进行有界模型验证；
3. 消融语义前提，搜索等价性反例；
4. 检查不支持构造是否产生 `UNKNOWN`、RAE 是否可重放以及实现是否存在隐藏 guard。

若某项前提没有必要性反例，可以作为保守充分条件保留，但必须报告其导致的可实例化空间损失。

### 5.2 外部基线与内部消融

GRev 是图数据库等价查询重写的直接基线。比较应限定在共同 GDBMS、共同语言子集、相同图和查询预算上。Dinkel 是最接近的细粒度变换工作；若其正式制品和当前目标系统可复现，应比较可转换查询比例、有效查询对和逻辑缺陷产出，但不得把无法兼容的单元格计为零产出。`[MATERIAL GAP: 完成两个制品的可复现实验和公平共同子集协议。]`

内部消融包括：

- `AQCG` 对比 `AST-local`；
- obligation coverage 对比 uniform-safe；
- 删除单个语义义务的 guard ablation；
- 删除事实来源或下游观察边的上下文消融。

运行时间、执行计划和已知 issue 反馈不能进入任何正式静态覆盖变体。

### 5.3 性能实验

性能实验在固定图快照、参数、数据库版本、驱动和硬件上进行。每个查询对采用随机化配对顺序，先预热，再形成多个独立执行块。正式阈值由独立 pilot 冻结。主结果报告效应量、绝对差、方向稳定性和区间估计，而不是只报告最大倍率。

`[RESULT GAP: 确定目标 GDBMS、版本、图规模、每系统查询预算、随机种子数和硬件配置。]`

## 6 当前累计问题结果

当前公开问题集合包含29个 issue reports，覆盖 Neo4j、ArcadeDB、LadybugDB、Memgraph、Apache AGE 和 FalkorDB。按问题类型划分为25个性能问题和4个逻辑问题。

按用户在 2026-07-27 提供的当前维护状态，工作统计如下：

| GDBMS | Reports | Performance | Logical | Fixed | Confirmed | Waiting | Uninterested |
|---|---:|---:|---:|---:|---:|---:|---:|
| Neo4j | 7 | 6 | 1 | 1 | 5 | 0 | 1 |
| ArcadeDB | 6 | 5 | 1 | 6 | 0 | 0 | 0 |
| LadybugDB | 7 | 5 | 2 | 5 | 2 | 0 | 0 |
| Memgraph | 5 | 5 | 0 | 0 | 1 | 4 | 0 |
| Apache AGE | 3 | 3 | 0 | 0 | 1 | 2 | 0 |
| FalkorDB | 1 | 1 | 0 | 0 | 0 | 1 | 0 |
| **Total** | **29** | **25** | **4** | **12** | **9** | **7** | **1** |

由此得到当前累计工作值：

\[
B_{\mathrm{total}}=21
=4\text{ logical}+17\text{ performance}.
\]

其中：

\[
B_{\mathrm{total}}
=
\left|
\{\text{去重后且被开发者 confirmed/fixed 的逻辑或性能问题}\}
\right|.
\]

`[MATERIAL GAP: 当前仓库中的 2026-07-22 快照记录为15个 confirmed/fixed，与用户最新维护表不一致。投稿前必须逐项重新打开29个 issue，核对开发者评论、修复提交和状态日期；在完成核查前，21只能作为工作值。]`

现有29个 issue 可按语义触发抽象为七类开发期问题族：

| 问题族 | Reports | 目标层级 |
|---|---:|---|
| 布尔恒等嵌入 | 12 | 表达式/谓词 |
| 图模式合取排列 | 4 | 图模式 |
| 约束表示与物化 | 5 | 模式—谓词 |
| 聚合与基数保持 | 4 | 聚合/作用域 |
| 零跳路径折叠 | 2 | 图拓扑 |
| 已绑定节点强化 | 1 | 作用域/模式 |
| 只读 `UNION` 分支排列 | 1 | 查询组合 |

这些 issue 可以用于说明问题的现实性和重放冻结后的规则，但不能被描述为未见测试集。对于规则覆盖，逐项标记：

- `DIRECT`：冻结后的 CORE 规则直接生成该查询对；
- `RECONSTRUCTABLE`：经语义保持归约后可由 CORE 规则生成；
- `INSPIRATIONAL`：只启发规则，冻结后的 CORE 尚不能生成。

只有实际完成重放的 `DIRECT` 和 `RECONSTRUCTABLE` 条目才能计入冻结框架的 issue 覆盖；累计 bug 总数仍按开发者 `confirmed/fixed` 的统一标准报告。

`[RESULT GAP: 完成29个 issue 的版本化重放、规则映射和 DIRECT/RECONSTRUCTABLE/INSPIRATIONAL 审计。]`

## 7 有效性威胁

**构造效度。** `SAFE` 表示规则定理前提在当前分析域中成立，不表示查询对一定适合跨运行比较。GraphSymPerf 通过独立 Oracle 执行资格门排除非确定性和环境漂移。性能不对称也不自动等于性能 bug，只有开发者确认或修复的问题进入累计 bug 指标。

**内部效度。** AQCG 事实推导、符号解析或 RAE 实现错误可能错误接受位点。有界小模型、结构候选完备性检查、前提消融和 RAE 重放用于降低该风险，但不能替代规则证明和代码审查。性能实验还受到缓存、JIT、后台负载和连接状态影响，因此需要随机化配对、绝对差门槛和独立复现。

**外部效度。** CORE-v1 只覆盖 \(\mathcal L_A\) 和每条规则的 \(D_r\)。六个有 issue 记录的 GDBMS 不能代表所有图数据库或 GQL 实现。方言适配器能够隔离语法差异，但不能自动消除各厂商可观察语义差异。

**结论效度。** 29个 issue 参与了问题族和规则候选抽象，因此不能作为完全独立、无偏的测试集。本文将其用于累计实证证据和冻结后重放，不据此计算无偏发现率。正式有效性结论还需要固定预算下的新查询实验、区间估计和消融。

**相关工作定位。** Dinkel 已经使用查询上下文保证生成有效性，并实现细粒度表达式和子句变换；GRev 已经提出 ASG 和等价查询重写。若 GraphSymPerf 最终不能证明显式证明义务、RAE 或非局部上下文分析带来可测收益，则贡献只能被描述为工程化重构，而不足以支撑当前核心论点。

## 8 讨论

### 8.1 为什么不是为每个 GDBMS 设计规则

GraphSymPerf 采用共享语义核心和方言适配层：

\[
Q_D
\xrightarrow{Frontend_D}
AQCG
\xrightarrow{CORE}
AQCG'
\xrightarrow{Backend_D}
Q'_D.
\]

具体系统只提供解析、序列化、Schema 读取、执行接口和方言语义事实。CORE 规则禁止根据 GDBMS 名称分支。若某项规则必须依赖厂商专属逻辑，它只能进入扩展规则集，不能支撑通用性主张。

### 8.2 RAE 的可解释性边界

本文所称“可解释”是决策依据忠实、可追溯且可重放，不主张经过用户研究验证的人类可理解性。RAE 的自然语言说明不能添加结构化事实中不存在的理由。若未来要主张可读性或调试效率，需要单独的人类参与评估。

### 8.3 充分条件与完备性

GraphSymPerf 的目标是 sound-oriented safe instantiation，而不是证明找到所有安全位点。候选匹配器只对规则结构模式 \(M_r\) 具有相对完备性；证明义务可以采用保守充分条件。论文应报告这些条件引入的 `UNKNOWN` 和丢失的可实例化空间，不能把保守性隐藏为语言本身不支持。

## 9 可复现性、伦理与声明

**Reproducibility.** 正式制品应包含版本化 AQCG Schema、规则规范、定理与证明、方言语义描述、查询与图快照哈希、RAE、覆盖状态、Oracle 配置、原始配对时间和 issue 映射。`[MATERIAL GAP: 确定匿名制品仓库、许可证和归档策略。]`

**Ethics.** 研究使用合成图和授权的本地测试实例，不涉及人类受试者、动物实验或个人敏感数据。问题通过上游公开 issue tracker 负责任报告，不对生产系统执行负载测试。

### Data Availability

`[MATERIAL GAP: 填写图快照、查询对、RAE、计时记录和 issue 映射的公开范围与归档地址。]`

### Code Availability

`[MATERIAL GAP: 填写最终仓库、匿名链接、许可证和版本归档。]`

### Conflicts of Interest

`[MATERIAL GAP: 作者填写。]`

### Funding

`[MATERIAL GAP: 作者填写资助来源和项目号。]`

### CRediT Contributions

`[MATERIAL GAP: 按作者角色填写。]`

### AI Use Disclosure

生成式 AI 用于研究设计讨论、初稿组织和语言检查。全部方法决策、实验、结果、引用和最终文字由作者复核并承担责任。投稿前按目标 venue 当年政策调整披露。

## 10 结论

本文将图数据库等价查询测试中的规则应用重新表述为上下文可证明的安全实例化问题。GraphSymPerf 通过 AQCG 表达跨子句和图模式的语义依赖，通过声明式证明义务连接规则级条件等价定理和实例级 RAE，并在 `SAFE` 实例空间中进行纯静态覆盖。逻辑 Oracle 检查严格结果不一致，性能 Oracle 只在结果一致后检测稳定、可复现的配对性能不对称。当前29个公开 issue 说明该问题具有现实相关性，但完整方法、统一消融和冻结后的跨系统实验仍需完成。论文能否成立，最终取决于 CORE 规则闭环、RAE 忠实性、AQCG 相对局部 AST 的增益以及固定协议下的新实证证据，而不是累计 issue 数量本身。

## 参考文献

1. Ziyue Hua, Wei Lin, Luyao Ren, Zongyang Li, Lu Zhang, Wenpin Jiao, and Tao Xie. 2023. *GDsmith: Detecting Bugs in Cypher Graph Database Engines*. ISSTA 2023. DOI: [10.1145/3597926.3598046](https://doi.org/10.1145/3597926.3598046). `hua2023gdsmith`
2. Matteo Kamm, Manuel Rigger, Chengyu Zhang, and Zhendong Su. 2023. *Testing Graph Database Engines via Query Partitioning*. ISSTA 2023. DOI: [10.1145/3597926.3598044](https://doi.org/10.1145/3597926.3598044). `kamm2023gdbmeter`
3. Zeyang Zhuang, Penghui Li, Pingchuan Ma, Wei Meng, and Shuai Wang. 2023. *Testing Graph Database Systems via Graph-Aware Metamorphic Relations*. PVLDB 17(4). DOI: [10.14778/3636218.3636236](https://doi.org/10.14778/3636218.3636236). `zhuang2023gamera`
4. Qiuyang Mang, Aoyang Fang, Boxi Yu, Hanfei Chen, and Pinjia He. 2024. *Testing Graph Database Systems via Equivalent Query Rewriting*. ICSE 2024. DOI: [10.1145/3597503.3639200](https://doi.org/10.1145/3597503.3639200). `mang2024grev`
5. Celine Wüst, Zuming Jiang, and Zhendong Su. 2026. *Dinkel: State-Aware and Granular Framework for Validating Graph Databases*. PVLDB 19(6), 1198–1211. DOI: [10.14778/3797919.3797928](https://doi.org/10.14778/3797919.3797928). `wust2026dinkel`
6. Xinyu Liu, Qi Zhou, Joy Arulraj, and Alessandro Orso. 2022. *Automatic Detection of Performance Bugs in Database Systems Using Equivalent Queries*. ICSE 2022. DOI: [10.1145/3510003.3510093](https://doi.org/10.1145/3510003.3510093). `liu2022amoeba`
7. Lei Tang, Wensheng Dou, Yingying Zheng, Lijie Xu, Wei Wang, Jun Wei, and Tao Huang. 2025. *Proving Cypher Query Equivalence*. ICDE 2025. DOI: [10.1109/ICDE65448.2025.00293](https://doi.org/10.1109/ICDE65448.2025.00293). `tang2025graphqe`

## 附录 A：初稿完成缺口

### 方法实现

- `[MATERIAL GAP]` 统一 AQCG 构建器和事实推导。
- `[MATERIAL GAP]` 证明义务 DSL、三值求值器和 RAE。
- `[MATERIAL GAP]` 证明义务驱动的单因素与成对覆盖。
- `[MATERIAL GAP]` 8条优先规则的逐条证明、实现、小模型验证和前提消融。
- `[MATERIAL GAP]` 清理重复 `SafetyCertificate`，统一术语为 RAE。

### 实验

- `[RESULT GAP]` RQ1 全部规则验证。
- `[RESULT GAP]` AQCG 与 AST-local 消融。
- `[RESULT GAP]` obligation coverage 与 uniform-safe 消融。
- `[RESULT GAP]` 独立性能 pilot 和阈值冻结。
- `[RESULT GAP]` 固定预算的跨系统逻辑与性能实验。
- `[RESULT GAP]` GRev/Dinkel 公平共同子集复现。
- `[RESULT GAP]` 29个 issue 的版本化重放与规则映射。

### 写作与证据

- `[MATERIAL GAP]` 实时复核29个上游 issue 状态，解决15与21的快照冲突。
- `[MATERIAL GAP]` 确定目标 venue 及当年模板、页数、双盲和 AI 政策。
- `[MATERIAL GAP]` 生成正式 `.bib` 并完成100%引用审计。
- `[MATERIAL GAP]` 完成方法图、AQCG 图、RAE 示例和评估表。

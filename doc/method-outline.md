# GraphSymPerf 方法章节大纲

## 1. 方法概述与研究边界

### 1.1 方法概述

GraphSymPerf 是一种基于等价查询重写（Equivalent Query Rewriting，EQR）的图数据库管理系统（Graph Database Management System，GDBMS）性能测试方法。其总体流程遵循数据库自动化测试的常见组织方式：生成由属性图和基础查询构成的测试输入；利用保持语义的重写规则生成重写查询；通过规则前置条件检查和结果等价预言验证查询对；比较同一 GDBMS、同一版本和同一图状态下等价查询的执行时间，并报告潜在性能缺陷。

该方法要解决的核心问题是：等价查询性能测试需要生成具有不同语法形态的等价查询对，但 Cypher 的图模式变量绑定、子句作用域、空值传播、路径和多重集语义以及跨系统方言差异，使一般的语义保持重写规则不能无条件应用。GraphSymPerf 的方法增量因此位于等价查询重写阶段：为每个重写规则显式检查图查询特有的规则前置条件，并将通过检查的查询对交给标准的结果等价预言和性能预言。

### 1.2 方法组成与技术增量

1. **依据图查询上下文实例化重写规则。** 方法将三值逻辑冗余谓词规则限制在受支持的普通 `MATCH` 谓词位置，并依据该位置的已绑定变量、属性上下文和目标方言选择可表达的布尔表达式，从而生成满足当前查询作用域的重写查询。
2. **联合实施规则前置条件检查与结果等价预言。** 重写阶段检查变量绑定、子句作用域、支持片段、目标方言兼容性和三值逻辑语义保持性，执行阶段比较完整且类型敏感的查询结果；只有同时通过两项验证的等价查询对进入性能测试。
3. **采用与执行计划解耦的配对性能预言。** 候选检测仅依据普通查询执行时间，通过预热和种子化 `AB/BA` 配对执行降低固定顺序、缓存与环境漂移造成测量偏差的风险，并将性能预言、执行计划分析和缺陷确认区分为不同阶段。
4. **以经验证查询对为统计单位组织可审计测试过程。** 分别记录生成的基础查询、成功重写的查询对、通过结果等价预言的查询对、完成性能测量的查询对和性能缺陷候选，使查询生成有效性、重写成功率、验证淘汰率和候选产出能够独立报告。

主要技术增量集中于前两项，即在图模式变量绑定、子句作用域、三值逻辑和目标方言共同约束下实例化等价查询，并联合实施规则适用性检查和结果等价观察。配对测量和分阶段记录属于保障性能证据可靠性与可审计性的实验设计，不单独构成新型数据库测试预言。

从测试方法分类看，基础查询和重写查询分别对应蜕变测试（Metamorphic Testing）中的源测试用例（source test case）和后续测试用例（follow-up test case），二者的结果等价关系构成蜕变关系（metamorphic relation）[4]。在数据库测试文献中，本文采用更直接的“基础查询”“等价查询重写”“重写查询”“验证规则”和“性能预言”等术语 [2], [10]。

### 1.3 研究范围与贡献边界

- 三值逻辑恒等式、等价查询重写和等价查询性能比较均已有研究基础，本文贡献不在于重新提出这些基本概念；
- GDsmith 仅承担属性图与基础查询生成，不构成本文的中心创新 [1]；
- 当前方法只处理有限的只读 Cypher 片段，不构成完整 Cypher 或图查询语言（Graph Query Language，GQL）的通用等价判定系统；
- 结果等价预言通过只说明查询对在当前系统和图快照上的外部观察一致，不能替代重写规则的语义保持依据；
- 性能预言达到倍率阈值只产生性能缺陷候选，不等同于已确认性能缺陷；
- 当前自动化主流程止于性能缺陷候选记录；独立复现、测试用例约简、上游报告和开发者确认属于后续候选验证流程。

---

## 2. 方法

### 2.1 问题定义与标准术语

#### 2.1.1 测试输入与等价查询对

给定：

- 被测系统（System Under Test，SUT）\(S\)，即目标 GDBMS 实例；
- 固定属性图快照 \(G\)；
- 基础查询（base query）\(Q\)；
- 候选重写位置 \(s\)；
- 冗余表达式 \(E\)；
- 目标方言 \(d\)；
- 语义保持重写规则 \(r=(c,t)\)，其中 \(c\) 为规则前置条件，\(t\) 为查询变换；
- 重写查询（rewritten query）\(Q'=t(Q,s,E)\)。

属性图和基础查询共同构成测试输入 \(I=(G,Q)\)，这一用法与 GDsmith 对图数据库测试输入的定义一致 [1]。基础查询 \(Q\) 经等价查询重写产生 \(Q'\)；\((Q,Q')\) 构成候选等价查询对。研究目标是在相同图状态、相同 GDBMS 版本和相同外部执行条件下，从经验证的等价查询对中识别 \(Q'\) 相对于 \(Q\) 的方向性执行时间增长。

#### 2.1.2 重写规则适用条件

Amoeba 将语义保持重写规则表示为规则标识、规则条件和变换三元组，并仅在输入满足规则条件时实施变换 [10]；蜕变关系也可以带有限制源测试用例的前置条件 [4]。沿用这一术语，定义规则 \(r\) 对基础查询 \(Q\) 的适用条件：

\[
\begin{aligned}
\operatorname{Applicable}(r,Q,s,E,d)\iff{}&
\operatorname{Pure}(Q)\\
&\land \operatorname{OrdinaryMatch}(s)\\
&\land \operatorname{Bound}(E,s)\\
&\land \operatorname{SupportedFragment}(Q,s)\\
&\land \operatorname{DialectCompatible}(r,E,d)\\
&\land \operatorname{SemanticsPreserving}(r).
\end{aligned}
\]

其中：

- \(\operatorname{Pure}(Q)\) 表示查询只读，且不包含已识别的非确定行为或不稳定结果形态；
- \(\operatorname{OrdinaryMatch}(s)\) 表示变换位置属于当前支持的普通 `MATCH` 子句；
- \(\operatorname{Bound}(E,s)\) 表示新增表达式只引用目标图模式已经绑定的变量；
- \(\operatorname{SupportedFragment}(Q,s)\) 表示查询作用域、路径结构和谓词形态属于当前建模范围；
- \(\operatorname{DialectCompatible}(r,E,d)\) 表示表达式和重写形式可由目标方言无损表达；
- \(\operatorname{SemanticsPreserving}(r)\) 表示规则在 Cypher 三值逻辑域中保持原谓词值。

#### 2.1.3 等价查询验证条件

规则适用性检查定义为：

\[
\operatorname{RewriteValid}(Q,Q')
\iff
\operatorname{Applicable}(r,Q,s,E,d)
\land Q'=t(Q,s,E).
\]

执行与结果等价验证定义为：

\[
\begin{aligned}
\operatorname{ResultValid}_{S,G}(Q,Q')\iff{}&
\operatorname{Executable}_{S,G}(Q,Q')\\
&\land \operatorname{CompleteResult}_{S,G}(Q,Q')\\
&\land \operatorname{TypedEq}_{S,G}(Q,Q').
\end{aligned}
\]

经验证的等价查询对满足：

\[
\operatorname{ValidPair}_{S,G}(Q,Q')
\iff
\operatorname{RewriteValid}(Q,Q')
\land \operatorname{ResultValid}_{S,G}(Q,Q').
\]

该定义区分重写规则的语义保持依据和被测系统上的结果等价观察：前者说明为何可以生成 \(Q'\)，后者说明 \((Q,Q')\) 是否可作为当前性能测试的有效测试用例。结果等价观察不能反向补足不满足前置条件的查询重写。

#### 2.1.4 性能预言与潜在性能缺陷

第 \(i\) 组配对观测定义为：

\[
\rho_i=\frac{T_i(Q')}{T_i(Q)},\qquad
\Delta_i=T_i(Q')-T_i(Q).
\]

查询对的中位运行时间倍率为：

\[
R(Q,Q')=\operatorname{median}_{i=1}^{m}\rho_i.
\]

当前测试协议取 \(m=5\) 和 \(\tau=2.0\)。运行时性能预言（runtime-based performance oracle）为：

\[
\begin{aligned}
\operatorname{PerfOracle}_{\tau}(Q,Q')\iff{}&
\operatorname{ValidPair}_{S,G}(Q,Q')\\
&\land \operatorname{CompleteMeasurement}(Q,Q')\\
&\land R(Q,Q')\ge\tau.
\end{aligned}
\]

满足该性能预言的查询对被记录为潜在性能缺陷（potential performance bug）[10]，本文在统计时称其为性能缺陷候选。阈值 \(\tau\) 仅用于低成本候选检测，不是通用的性能缺陷判定标准。该预言也不包含独立复现、统计置信下界、绝对噪声下限、根因确认或开发者确认。

### 2.2 数据库测试总体流程

本文按数据库自动化测试文献中的常见阶段组织方法。GDsmith 将属性图与查询共同定义为图数据库测试输入，并按“生成属性图—生成 Cypher 查询—执行查询”的过程开展测试 [1, Sec. 2.3 and Alg. 1]。GRev 的总体架构包括图生成器（Graph Generator）、基础查询生成器（Base Query Generator）、等价查询重写器（Equivalent Query Rewriter）和验证规则（Validation Rules）[2, Fig. 4 and Sec. 4.4]。Amoeba 则将等价查询性能测试划分为生成器（Generator）、变异器（Mutator）和验证器（Validator），其中变异器应用满足前置条件的语义保持查询重写规则，验证器执行等价查询并比较运行时间 [10, Fig. 2 and Sec. 7]。据此，GraphSymPerf 的当前自动化主流程分为以下五个阶段：

1. **测试输入生成（Test Input Generation）：** 依次进行属性图生成（Property Graph Generation）和基础查询生成（Base Query Generation），得到相互匹配的属性图与只读 Cypher 基础查询，并在目标 GDBMS 中加载相应属性图；
2. **等价查询重写（Equivalent Query Rewriting）：** 选择受支持的重写位置，在满足规则前置条件时应用语义保持重写规则，生成重写查询；
3. **等价查询验证（Validation）：** 检查规则适用性，并在固定图快照上执行基础查询与重写查询，使用结果等价预言验证完整、类型敏感的查询结果；
4. **性能测量（Performance Measurement）：** 对经验证的等价查询对执行预热和种子化 `AB/BA` 配对计时；
5. **性能预言（Performance Oracle）：** 根据完整配对测量和运行时间倍率识别潜在性能缺陷，并保存可用于后续复现的测试制品。

当前自动化主流程可表示为：

\[
\text{Property Graph Generation}
\rightarrow
\text{Base Query Generation}
\rightarrow
\text{Equivalent Query Rewriting}
\rightarrow
\text{Validation}
\rightarrow
\text{Performance Measurement}
\rightarrow
\text{Performance Oracle}
\rightarrow
\text{Potential Performance Bug}.
\]

候选形成后的标准处理过程为：

\[
\text{Candidate Confirmation}
\rightarrow
\text{Test-Case Reduction}
\rightarrow
\text{Bug Reporting}.
\]

独立复验和测试用例约简目前属于候选形成后的离线验证过程，尚未纳入候选发现的端到端执行路径。因此，二者用于评估候选确认阶段，不构成当前候选发现流程的必经步骤。

本文采用的数据库测试术语、定义及文献依据如下：

| 本文术语 | 含义 | 主要文献依据 |
|---|---|---|
| 属性图生成、基础查询生成 | 生成组成测试输入的属性图与基础查询 | GDsmith [1]、GRev [2] |
| 基础查询、重写查询 | 重写前后的查询 | GRev [2]、Amoeba [10] |
| 等价查询重写 | 通过语义保持规则构造等价查询 | GRev [2]、Amoeba [10] |
| 规则前置条件 | 限制重写规则适用范围的条件 | Amoeba [10]、蜕变测试综述 [4] |
| 结果等价预言 | 比较基础查询和重写查询的完整结果 | GRev [2]、蜕变测试综述 [4] |
| 性能预言 | 通过等价查询的执行时间差识别潜在性能缺陷 | Amoeba [10] |
| 潜在性能缺陷 | 通过性能预言但尚未完成缺陷确认的测试用例 | Amoeba [10] |

各阶段构成单向验证链：测试输入生成提供图和基础查询；等价查询重写负责给出重写规则的语义依据；等价查询验证检查当前 SUT 和图快照上的可执行性与结果等价性；性能测量和性能预言仅作用于经验证的等价查询对。后续观察到的性能差异、执行计划或历史 issue 均不能反向补足不成立的规则前置条件，也不能将结果不一致的查询对解释为等价查询性能问题。

**设计原则：**

- 只在同一系统、同一版本和同一图状态内比较等价查询，不以跨系统运行时间作为性能判据；
- 候选检测只依赖普通执行时间，执行计划与剖析信息只用于候选形成后的诊断；
- 对不满足规则前置条件或结果等价预言的查询对采取保守拒绝，不通过删除查询结构或弱化结果比较提高产出；
- 生成的基础查询数不等于有效样本量，只有经验证的等价查询对才进入性能测量的样本分母。

### 2.3 测试输入生成：属性图生成与基础查询生成

#### 2.3.1 图数据库测试输入生成的难点

图查询的可执行性与图拓扑、标签、关系类型、属性值和查询模式共同相关。若独立生成图与查询，容易产生模式不满足、属性缺失或大量空结果，进而降低等价查询重写和性能测量的有效产出。

#### 2.3.2 属性图与基础查询生成

当前流程复用 GDsmith 的图引导和数据引导生成能力，获得相互匹配的属性图构造语句和只读 Cypher 基础查询 [1]。每个实验分片在目标系统上建立空图状态后，加载节点、标签、属性、关系方向、关系类型和关系属性，随后在真实目标 GDBMS 上执行基础查询和重写查询。

对于缺少 GDsmith 原生生成后端的目标系统，兼容代理只承担输入生成职责；图状态重放、结果比较和性能测量仍在真实目标系统上完成。实验结果必须区分原生生成与代理生成，以控制输入分布差异这一替代解释。

#### 2.3.3 目标系统加载与适用边界

目标适配遵循语义保持的加载原则：保持属性图结构、查询图模式与谓词语义、基础查询与重写查询之间的对应关系，以及影响选择性和中间结果基数的查询结构。无法无损表达标签、关系类型、谓词、作用域或路径结构时拒绝输入。

采用属性图与查询协同生成能够提高基础查询与图状态的匹配程度，但生成分布不必然代表真实工作负载。GDsmith 在本研究中承担测试输入生成职责；生成器偏差构成外部效度限制，不属于本文的方法贡献。

### 2.4 带规则前置条件的等价查询重写（Equivalent Query Rewriting）

#### 2.4.1 图查询重写的特有前置条件

相较于不涉及图模式的局部谓词重写，在 Cypher 查询中应用语义保持规则同时受到以下约束：

1. **图模式变量绑定。** 新增表达式只能引用目标图模式已经绑定的节点变量；否则局部恒等式在具体查询中可能不可解析或改变作用域。
2. **子句与子查询作用域。** `WITH`、子查询、`EXISTS` 等结构可能重绑定、隐藏或引入变量，单纯文本匹配不能可靠确定表达式可见性。
3. **三值逻辑与空值传播。** 缺失属性和可选匹配可能产生 `null`，经典二值逻辑恒等式不能直接迁移到 Cypher 谓词语义 [7], [8]。
4. **路径与多重集语义。** 路径枚举、连接和图模式匹配可能改变行基数与重复项，结果集合相同不足以说明完整查询行为一致。
5. **目标方言差异。** 各 GDBMS 对函数、表达式及图对象表示的支持范围不同，规则语义成立并不意味着所有目标系统均能无损执行。

因此，逻辑恒等式只是定义重写规则的必要基础，而不是规则可用于任意 Cypher 查询的充分条件。GraphSymPerf 在等价查询重写阶段显式检查上述规则前置条件，以判断该规则能否用于当前查询位置和目标方言。

#### 2.4.2 基础查询资格检查

等价查询重写阶段排除：

- 写操作；
- 已识别的随机、时间或其他非确定函数；
- 缺少稳定 `ORDER BY` 的 `LIMIT` 或 `SKIP`；
- 缺少稳定顺序保证的 `collect()`；
- 以 `WITH` 或 `OPTIONAL MATCH` 开始的查询；
- `CALL`、`EXISTS`、模式推导式和变长路径；
- 不含受支持普通 `MATCH`，或目标图模式未绑定节点变量的查询；
- 超出当前查询表示或目标方言能力的结构。

这些条件分别控制副作用、跨执行结果漂移、变量作用域变化、空值扩展、路径枚举和方言不兼容风险。被排除的结构可能存在语义保持的重写方法，但不属于当前规则能够给出充分适用性依据的支持域。

#### 2.4.3 等价查询重写过程

给定基础查询 \(Q\)、目标方言 \(d\) 和由实验标识确定的选择索引 \(k\)，重写过程在规则前置条件成立时输出查询对 \((Q,Q')\)，否则将该规则应用标记为不适用。当前方法依次执行以下步骤。

**步骤 1：查询资格判定。**

检查 \(Q\) 是否只读、结果形态是否稳定，以及查询是否可由当前目标执行路径执行。包含写操作、已识别的非确定函数、不稳定限制操作或不受支持作用域结构的查询不进入等价查询重写。

该步骤位于语法变换之前，原因是即使局部规则本身成立，副作用或跨执行不确定性仍会破坏查询对作为性能比较单位的有效性。

**步骤 2：重写位置选择。**

将查询划分为顶层子句，并选择其中第一个普通 `MATCH` 子句作为候选位置 \(s\)。随后要求该子句的图模式能够被解析为当前支持的有限抽象语法结构，包括具名节点、定长关系和由此组成的模式列表。

若不存在普通 `MATCH`、图模式超出可证明片段或包含变长路径，则拒绝该查询。当前仅选择第一个受支持 `MATCH`，属于确定性且保守的覆盖策略；它避免多个候选位置组合引入额外证明责任，但也限制了方法对后续 `MATCH` 子句的覆盖。

**步骤 3：提取局部上下文。**

从目标图模式中按出现顺序提取已绑定节点变量集合：

\[
B_s=\operatorname{BoundVars}(Q,s).
\]

当前方法确定性地选择其中第一个节点变量 \(v^\ast\) 作为冗余表达式的宿主变量。若目标子句已有 `WHERE`，则令其原谓词为 \(P\)；否则令：

\[
P=\mathsf{true}.
\]

同时，从 \(Q\) 中收集与 \(v^\ast\) 关联的已出现属性名，形成局部属性上下文 \(A(Q,v^\ast)\)。这一过程使规则应用受当前图模式的变量绑定和属性上下文约束，而不是从全局变量名或任意文本片段中选择表达式。

**步骤 4：生成目标方言可表达的布尔表达式。**

根据 \(v^\ast\)、属性上下文 \(A(Q,v^\ast)\)、选择索引 \(k\) 和目标方言 \(d\)，从受限表达式集合中确定性选择 \(E\)：

\[
E=\operatorname{SelectExpr}\!\left(v^\ast,A(Q,v^\ast),k,d\right).
\]

受限集合只包含具有结构化表示、变量依赖可追踪且复杂度受限的布尔表达式，包括节点标识或标签相关恒等式、属性空值分区和保守字面量等类别。对于非 Neo4j 方言，候选集合以可移植表达式为主，并包含字面量恒真式。若所选表达式未通过最终方言检查，则放弃本次规则应用，而不是通过删除原查询结构获得兼容性。

表达式选择由实验标识确定，而不依赖运行时间、执行计划或开发者反馈。这一约束避免候选生成受到性能结果反向引导。

**步骤 5：应用语义保持重写规则。**

对当前正式重写规则族，默认构造：

\[
P'
=
P\land
\left(E\lor\neg E\lor\operatorname{isNull}(E)\right).
\]

对于当前不能稳定表达该复合保护式的目标方言，使用由同一证明过程验证的更保守恒真形式。现有 ArcadeDB 适配采用：

\[
P'=P\land\mathsf{true}.
\]

因此，目标适配改变的是规则在目标方言中的具体语法形式，而不是“重写后谓词必须与 \(P\) 等值”这一语义保持要求。

**步骤 6：检查规则前置条件与语义保持性。**

重写规则只有同时满足以下条件时才应用于当前查询：

1. 目标位置属于受支持的普通 `MATCH` 图模式片段；
2. \(E\) 具有受支持的布尔表达式结构；
3. \(\operatorname{Vars}(E)\subseteq B_s\)；
4. 规则对 \(P,E\in\{\mathsf{true},\mathsf{false},\mathsf{null}\}\) 的全部 \(3\times3\) 组合均满足 \(P'=P\)；
5. 将恒真保护式化简后，重写前后的局部选择均规范化为同一关系代数表示。

任一前置条件或语义保持要求不成立时，当前规则均不应用于该查询，而不是依赖一次执行中结果相同将其补充为有效重写。当前检查对象为受支持图模式上的局部选择条件：原谓词 \(P\) 被视为保持不变的选择条件，证明对象是附加保护式不会改变 \(P\)，而不是重新证明 \(P\) 自身的语义。

**步骤 7：结构保持地构造重写查询。**

若目标 `MATCH` 已含 `WHERE P`，则仅将该区域替换为 `WHERE P'`；若不存在 `WHERE`，则在该 `MATCH` 末尾增加 `WHERE P'`。目标子句之外的查询前缀、后续子句、投影及排序结构均保持不变。

构造完成后，对 \(Q\) 和 \(Q'\) 再实施目标方言兼容性与执行能力检查。只有两条查询均可由目标执行路径处理时，重写过程才输出查询对；否则将该规则应用标记为方言不适用。

等价查询重写算法如下：

```text
RewriteEquivalentQuery(Q, d, k):
  reject unless Q is pure, stable, and executable by the target adapter
  s ← first supported ordinary MATCH in Q
  B_s ← bound node variables of the graph pattern at s
  v* ← first variable in B_s
  P ← WHERE predicate at s, or true if WHERE is absent
  E ← SelectExpr(v*, observed properties, k, d)
  r_d ← supported redundant-predicate rule for dialect d
  reject unless the rule preconditions and 3-valued semantic check hold
  Q' ← replace P with r_d(P,E), preserving all non-target query structure
  reject unless Q and Q' satisfy the target dialect and execution constraints
  return (Q,Q')
```

例如，对 Neo4j 查询

```cypher
MATCH (n:Person) WHERE n.age > 18 RETURN n
```

目标位置为第一个 `MATCH`，绑定变量集合为 \(B_s=\{n\}\)，原谓词为 \(P=(n.age>18)\)。若确定性表达式选择得到 \(E=(\operatorname{elementId}(n)=\operatorname{elementId}(n))\)，则重写后谓词为：

\[
P'
=
(n.age>18)
\land
\left(E\lor\neg E\lor\operatorname{isNull}(E)\right).
\]

最终只替换该 `MATCH` 的 `WHERE` 谓词，`MATCH (n:Person)` 与 `RETURN n` 均保持不变。该示例仅说明重写过程，实际表达式由目标方言、查询上下文与确定性选择索引共同决定。

对应的重写查询为：

```cypher
MATCH (n:Person)
WHERE (n.age > 18)
  AND (
    (elementId(n) = elementId(n))
    OR NOT(elementId(n) = elementId(n))
    OR (elementId(n) = elementId(n)) IS NULL
  )
RETURN n
```

该算法说明了规则前置条件并非对生成后的查询对进行事后筛选，而是直接约束重写位置、宿主变量、表达式、规则形式和最终查询的构造。

**可复现性边界。**

重写过程能够计算重写位置、已绑定变量、所选表达式和语义检查结果；当前保存的候选测试制品尚未完整包含这些信息，对目标系统实际采用的规则形式记录亦不充分。因此，现有测试制品支持对重写算法的整体复现，但不足以为每个候选提供可独立重放的完整证明证书。

#### 2.4.4 结构保持的局部谓词重写

方法识别顶层子句、普通 `MATCH`、定长图模式和目标 `WHERE` 区域，并只修改选定的谓词位置：

- 原子句含 `WHERE` 时，将原谓词 \(P\) 替换为 \(P'\)；
- 原子句不含 `WHERE` 时，令 \(P=\mathsf{true}\) 并增加谓词；
- 新表达式只引用目标图模式已绑定的变量；
- 其他查询子句保持不变。

该设计选择局部、结构保持的谓词重写，而不采用任意语法扰动，原因是前者能够明确限定证明责任，并尽量保持图模式、连接结构和中间结果基数等性能相关特征。方法描述抽象为结构保持的局部谓词重写，不涉及文本区间、字符串操作或具体解析器数据结构。

#### 2.4.5 三值逻辑冗余谓词

对支持相应表达式的目标系统，构造：

\[
P'
=
P\land
\left(E\lor\neg E\lor\operatorname{isNull}(E)\right).
\]

在 Cypher 三值逻辑域

\[
\{\mathsf{true},\mathsf{false},\mathsf{null}\}
\]

中：

\[
E\lor\neg E\lor\operatorname{isNull}(E)=\mathsf{true},
\]

因而：

\[
P'=P\land\mathsf{true}=P.
\]

当前语义检查对 \(P\) 与 \(E\) 的全部 \(3\times3\) 组合进行穷举，仅当规则对所有组合保持 \(P'=P\) 时才允许应用。该逻辑基础与三值逻辑查询分区（Ternary Logic Partitioning，TLP）及其图数据库应用相关 [8], [9]，但本文将其嵌入单条查询的冗余保护式，以构造性能比较查询对，而不是合并三个查询分区的结果来检测逻辑错误。

对于需要保守适配的目标方言，可采用由同一证明过程验证的恒真替代；实验设置按目标系统分别报告实际采用的规则形式。

**公开案例及其方法学作用。**

- LadybugDB #669 [I1] 报告的查询对在原谓词后增加了排中律及空值保护式，与上述正式主规则具有直接的语义对应关系。公开报告在给定图状态上获得相同结果，并记录约 \(7.975\times\) 的中位运行时间差。该案例表明，即使冗余项在三值逻辑下恒真，其具体语法形态仍可能暴露优化器对逻辑规范化、屏障或中间计划结构的敏感性。该 issue 当前已关闭，但公开讨论仅包含维护者要求基于拉取请求（pull request，PR）#671 复测的请求，未提供可核查的复测结果；其公开证据状态为“等待复测”，而非“已验证修复”。
- ArcadeDB #4405 [I2] 报告了 \((P\land\mathsf{true})\lor(\mathsf{false}\land E)\) 未在规划前化简的问题；对应修复 PR #4412 已合并。现有方法包含该方言特定规则，但正式实验协议并未将其作为 ArcadeDB 的首个重写规则。该案例支持按方言报告实际规则形式并分别验证规则前置条件与语义保持性，但不构成该规则已进入当前正式实验主路径的证据。

#### 2.4.6 方法增量与适用范围

本节的技术增量不是三值恒等式本身，而是**面向图查询的重写规则前置条件检查**：在等价查询重写阶段联合检查图模式变量绑定、Cypher 子句作用域、目标方言和三值逻辑语义保持性，再将成功重写的查询对交给执行驱动的结果等价预言。

其作用在于克服以下困难：

- 防止未绑定变量、错误作用域或不受支持结构使局部重写规则不适用于当前查询；
- 防止仅依据当前图上的偶然结果相同，将缺乏语义依据的查询对误作等价查询对；
- 在目标方言存在差异时，要求每种实际重写形式仍满足相同的语义保持要求；
- 为后续结果等价预言提供具有明确重写依据的查询对。

该保证只适用于当前支持的普通 `MATCH` 谓词片段，不承担完整 Cypher 查询等价判定，也不覆盖复杂子作用域、可选匹配、变长路径和未建模表达式。该定位区别于通用等价查询生成 [2]、图结构感知蜕变测试 [3]、状态感知图数据库验证 [5] 和完整查询等价推理 [6]。

### 2.5 等价查询验证与结果等价预言（Validation）

#### 2.5.1 重写规则前置条件检查

在执行查询对之前，方法依据第 2.4 节定义的规则前置条件检查：

- 基础查询是否只读且不存在已识别的非确定行为；
- 重写位置、变量作用域和路径结构是否属于当前支持域；
- 新增表达式是否仅引用目标图模式已绑定的变量；
- 重写规则及其表达式是否可由目标方言无损表达；
- 重写规则是否在三值逻辑域中保持原谓词语义。

该检查控制的是**重写规则不适用于当前基础查询**这一风险。它避免以一次执行中结果相同替代规则适用性依据，但不能观察目标系统在具体图状态上的执行失败、结果截断或实现偏差。

#### 2.5.2 执行有效性与结果等价验证

规则前置条件成立且重写查询生成后，在同一目标系统和固定图快照上普通执行 \(Q\) 与 \(Q'\)。结果等价预言要求：

- 两条查询均正常完成且未超时；
- 两条查询均返回完整、未截断的结果；
- 类型化结果一致；
- 对显式有序查询，类型化行序列一致；
- 对无显式顺序的查询，类型化行多重集一致。

不满足上述任一条件的查询对不进入性能测量。结果不一致可能来自方言语义、规则应用错误、结果规范化问题或潜在逻辑缺陷；在完成归因之前，不能将其统一表述为重写错误或 GDBMS 逻辑缺陷。

**结果不一致案例——LadybugDB #696。** LadybugDB #696 [I3] 中，两条仅交换逗号分隔图模式顺序的查询按预期均应返回计数 \(0\)，但在所报告版本上分别返回 \(0\) 和 \(1\)；对应 PR #702 将问题定位为变量先由无标签模式绑定后，后续显式标签约束丢失，并已合并修复。该案例不属于当前正式的局部谓词重写族。只要目标系统上的完整类型结果不同，该查询对即违反结果等价预言，因而退出性能测试并作为潜在逻辑异常单独调查；否则，运行时间差将与语义差异混合，无法解释为等价查询性能不对称。

#### 2.5.3 类型敏感的完整结果比较

结果规范化保留空值、布尔值、整数、浮点数、字符串、列表、映射及驱动特有对象的类型信息。比较规则为：

- 查询显式规定结果顺序时，比较类型化行序列；
- 未显式规定顺序时，比较类型化行多重集；
- 多重集保留重复行的出现次数。

该设计避免文本转换混淆类型、无序结果产生伪不一致，以及集合比较掩盖重复项和基数差异。当前顺序语义主要依据显式 `ORDER BY` 识别，驱动特有图对象的比较依赖适配器返回表示的稳定性；二者共同构成结果等价预言的适用边界。

#### 2.5.4 两类验证的互补性

| 验证组成 | 主要依据 | 排除对象 | 单独使用的局限 |
|---|---|---|---|
| 重写规则前置条件检查 | 语法、作用域、变量绑定、方言和三值逻辑语义保持性 | 规则不适用于当前查询的重写尝试 | 无法观察具体目标系统的执行行为 |
| 执行与结果等价验证 | 当前图快照上的完整类型结果 | 执行失败、结果截断或结果不一致的查询对 | 单独使用时可能接受仅在当前图上偶然结果相同的查询对 |
| 等价查询联合验证 | 规则适用性与外部可观察结果 | 具有明确重写依据且当前可执行、结果等价的查询对 | 不等于完整查询语言上的普遍等价证明 |

在缺少完备真实标签时，等价查询验证不能被解释为完全消除误报。可直接观测的指标包括各验证步骤的拒绝率、累计产出率、结果不一致率和独立验证存活率；总体假阳性率的估计需要额外的可信标签来源。

### 2.6 性能测量与运行时性能预言

#### 2.6.1 仅执行计时的性能测量

性能测量阶段仅使用普通查询执行接口。`EXPLAIN`、`PROFILE`、算子计数和开发者反馈均不参与性能预言，执行计划只用于候选形成后的诊断。

采用该设计的原因包括：

- 避免剖析开销改变被测查询的执行行为；
- 避免不同 GDBMS 的计划格式和算子语义差异进入统一候选判据；
- 将性能现象、根因假设和根因确认保持为相互区分的分析阶段。

Apache AGE #2428 [I5] 说明了这一分层的必要性。原报告观察到等价的空值矛盾分支改变了计划中的行估计；维护者在当前主分支上复验后确认结果等价，但指出所报告的“rows”差异属于估计基数变化，并在纯 PostgreSQL 中复现相同规划限制，因而不将其归为 AGE 缺陷。该案例表明，计划指标可以帮助解释候选，却不能替代普通执行时间成为跨系统统一判据；系统归因还必须通过独立复验和去除目标系统特有层的对照来建立。

以等价查询响应时间差异发现数据库性能问题的性能预言可追溯至 Amoeba [10]。Amoeba 的验证器执行等价查询、比较执行时间，并以随机顺序重新执行候选以进行确认；GraphSymPerf 当前主流程同样采用运行时间比较和随机次序，但不以执行计划差异作为候选进入条件。与面向跨版本性能回归检测与诊断的 APOLLO [13] 不同，GraphSymPerf 当前比较同一系统版本、同一图状态下的等价查询形态，并将执行计划分析置于候选形成之后。

#### 2.6.2 随机化配对执行

当前性能测量协议包括：

- 以固定 \(AB\) 次序执行 3 轮预热；
- 正式采集 5 组配对观测；
- 正式日程由 3 个 \(AB\) 与 2 个 \(BA\) 组成，并依据实验种子确定性随机排列；
- 任一预热或正式执行超时，则该查询对不形成完整性能测量。

配对执行使两条查询在相近时间窗口内接受测量，随机化正式执行顺序旨在缓解一条查询始终处于先执行或后执行位置造成的顺序偏差。种子化日程同时保证协议可复现。重复测量、预热和对执行不确定性的显式处理符合软件性能评估的一般要求 [11], [12]。

当前 5 组配对属于奇数预算，只能实现 3:2 的近似平衡；固定 \(AB\) 预热也可能保留预热阶段的次序效应。因此，随机化的实际增益不能由协议结构直接推导，必须通过固定顺序对照实验验证。

#### 2.6.3 性能预言与候选边界

性能预言使用中位运行时间倍率、中位绝对时间差和查询对测量完整性。当前正式判据为：

\[
\operatorname{PerfOracle}_{2.0}(Q,Q')
\iff
\operatorname{ValidPair}_{S,G}(Q,Q')
\land
\operatorname{CompleteMeasurement}(Q,Q')
\land
\operatorname{median}_{i=1}^{5}\rho_i\ge2.0.
\]

其中 \(2.0\) 只定义方向 \(Q\rightarrow Q'\) 上的候选检测阈值。五组配对仍可能受缓存、即时编译、系统负载和计时分辨率影响；当前性能预言也不要求绝对时间差超过经验噪声下限，且未使用具有统计解释的置信区间门槛。因此，性能缺陷候选必须与后续独立验证结果分开报告。

Neo4j #13813 [I4] 提供了“高倍率候选仍不等于性能缺陷”的直接案例。该报告中的逗号模式重排保持结果一致，但记录了约 \(206.34\times\) 的运行时间差和 \(293.04\times\) 的数据库访问差；维护者解释称，与图模式无关的谓词会确定性地附着到先求解的独立模式分量，并明确保留该行为。该案例位于当前局部谓词主规则之外，仅用于界定性能预言的证据能力：即使运行时间倍率较高、结果等价且可复现，开发者仍可能依据语言和规划器设计将其判定为预期行为。因此，性能预言只能触发后续调查，不能定义缺陷真值。


---

## 3. 方法组件的评估设计

本节规定上述方法主张应由何种实验回答，供后续评估章节使用，不构成预先写入的方法效果结论。

### 3.1 RQ1：查询生成与等价查询重写的有效性

**研究问题：**

> RQ1：在异构 Cypher GDBMS 上，测试输入生成和带规则前置条件的等价查询重写能够产生多少经验证的等价查询对？

**与方法创新的对应关系：**

- 变量绑定检查回答新增表达式能否在目标图模式中合法求值；
- 作用域与支持片段检查回答局部规则能否用于当前 Cypher 子句结构；
- 三值逻辑证明回答规则在存在 `null` 时是否仍保持原谓词；
- 方言约束回答同一语义规则能否在目标系统中无损表达；
- 属性图与基础查询协同生成减少查询模式与目标图不相容造成的无效输入。

**主要指标：**

\[
\operatorname{RewriteSuccessRate}
=
\frac{N_{\mathrm{rewritten}}}{N_{\mathrm{base}}},
\qquad
\operatorname{ValidatedPairYield}
=
\frac{N_{\mathrm{result\_equivalent}}}{N_{\mathrm{base}}}.
\]

**评估结果组成：**

- 各 GDBMS 的基础查询数、成功重写数、通过结果等价预言的查询对数及相应比例；
- 主要规则不适用类别及其比例；
- 原生生成与代理生成目标的分层结果；
- 被保守排除的 Cypher 结构及其对适用范围的影响。

**结论边界：**

- 较低的重写成功率可能来自保守的规则前置条件、目标方言能力或输入生成分布，不能直接解释为方法无效；
- 经验证查询对产出率只能表明方法的适用性和产出，不能单独证明规则对完整 Cypher 语言普遍成立；
- 跨系统适用性由分系统的重写成功率、验证通过率和实验完成状态共同刻画，绝对查询数不构成充分证据。

### 3.2 RQ2：等价查询验证各组成部分的作用

**研究问题：**

> RQ2：重写规则前置条件检查和执行驱动的结果等价验证分别排除了哪些无效查询对，并如何影响经验证查询对产出与跨系统适用性？

**阶段漏斗：**

\[
\begin{aligned}
N_0 &: \text{生成的基础查询},\\
N_1 &: \text{通过基础查询资格检查},\\
N_2 &: \text{满足规则前置条件并成功重写},\\
N_3 &: \text{目标方言可执行的查询对},\\
N_4 &: \text{结果完整且通过结果等价预言的查询对},\\
N_5 &: \text{完成配对性能测量的查询对},\\
N_6 &: \text{满足性能预言的性能缺陷候选}.
\end{aligned}
\]

对第 \(j\) 个阶段定义：

\[
\operatorname{Retention}_j=\frac{N_j}{N_{j-1}},
\qquad
\operatorname{CumulativeYield}_j=\frac{N_j}{N_0}.
\]

**评估设计：**

1. 报告完整阶段漏斗、逐阶段保留率和累计产出率；
2. 在受控样本上比较“仅使用结果等价观察”与“规则前置条件检查加结果等价验证”，检验规则前置条件是否能够减少仅在当前图上偶然结果相同的查询对；
3. 对放宽规则前置条件所得的查询对只统计其下游执行失败、结果截断和结果不一致，不将其纳入正式性能测试；
4. 抽样审查结果不一致案例，区分规则不适用、方言行为、结果规范化问题和潜在逻辑缺陷等替代解释；
5. 分系统报告各验证步骤的作用，以判断某一前置条件是在普遍提高有效性，还是对特定方言造成过度保守。

**误报率的术语约束：**

- 在没有完备真实标签时，不直接报告“总体假阳性率”；
- 可观察指标包括“规则不适用率”“执行失败率”“结果不一致率”和“独立验证存活率”；
- 误报率估计以预先定义的可信标签来源为条件，例如独立复验后人工审查、开发者明确否定或经验证的规则反例；未决样本单独报告；
- 开发者未回复不构成假阳性标签。

**可观测性限制：**

当前候选发现流程对执行阶段的拒绝原因已有分类，但查询重写不适用主要聚合为总体原因。若正式实验不补充具体规则不适用类别，RQ2 只能回答重写规则前置条件检查的整体作用，不能支持各项前置条件独立贡献的细粒度结论。

### 3.3 RQ3：随机化配对执行对性能预言稳定性的影响

**研究问题：**

> RQ3：在相同测量预算下，随机化 `AB/BA` 配对执行是否比固定执行顺序产生更稳定的性能缺陷候选？

**对照协议：**

1. 固定 \(AB\)；
2. 固定 \(BA\)；
3. 当前种子化随机 \(AB/BA\)。

三种协议使用相同的查询对集合、图快照、GDBMS 版本、预热次数、正式执行次数、超时条件、机器与外部负载控制，并在多个独立会话中重复。当前五组发现预算下，随机协议保持 3 个 \(AB\) 和 2 个 \(BA\)，只改变顺序安排；偶数配对预算可进一步实现严格平衡。

**主要指标：**

- 慢侧方向一致率；
- 候选判定的跨会话一致率；
- 运行时间倍率的相对离散程度；
- 性能缺陷候选的独立验证存活率。

**辅助指标：**

- 重复实验候选集合的 Jaccard 相似度；
- A/A 负对照误触发性能预言的比例；
- 固定 \(AB\) 与固定 \(BA\) 之间的方向性差异。

**可支持的结论：**

只有当随机协议在相同预算下表现出更高的方向一致性、更低的倍率离散或更高的独立验证存活率时，才能声称随机化提高了候选稳定性。若差异不成立，方法章节只能保留“随机化旨在缓解固定次序偏差”的设计表述。

**实验设计限制：**

当前候选发现流程提供随机日程，但尚无固定 \(AB\) 与固定 \(BA\) 的正式对照结果。RQ3 因而属于需要补充的受控评估，而不是现有证据已经支持的方法优势。

### 3.4 RQ4：缺陷发现有效性与外部验证

**研究问题：**

> RQ4：GraphSymPerf 产生的性能缺陷候选中，有多少能够通过独立复验和症状保持约简形成可报告测试用例；已提交报告中，有多少获得开发者确认或修复？

#### 3.4.1 缺陷报告状态定义

数据库测试研究通常以已提交、开发者确认和已修复的缺陷报告数量作为实际有效性的外部证据 [1]–[3], [10]。公开证据审计采用以下互斥状态：

| 状态 | 判定条件 | 统计符号 |
|---|---|---|
| 已提交（submitted） | 已完成上游去重并创建公开 issue | \(N_{\mathrm{submitted}}\) |
| 已确认但未修复（confirmed） | 维护者明确确认缺陷成立，但尚无可核查的修复证据 | \(N_{\mathrm{confirmed}}\) |
| 已修复（fixed） | 修复 PR 或提交已合并，或维护者明确给出包含修复的版本 | \(N_{\mathrm{fixed}}\) |
| 等待回复（pending） | 截止状态冻结日期尚无实质性维护者回复 | \(N_{\mathrm{pending}}\) |
| 未被采纳（not adopted） | 维护者将现象判定为预期行为、上游依赖、重复问题或暂不处理 | \(N_{\mathrm{not\_adopted}}\) |

issue 的打开或关闭状态本身不构成确认或修复证据。简单致谢、分派团队、添加标签或表示“将进行调查”也不单独满足开发者确认条件。confirmed 与 fixed 在状态快照中互斥；累计获得开发者正向验证的报告数定义为：

\[
N_{\mathrm{developer\_validated}}
=
N_{\mathrm{confirmed}}
+
N_{\mathrm{fixed}}.
\]

#### 3.4.2 项目状态快照与描述性统计

依据项目跟踪记录中的状态标注，截至 2026 年 7 月 29 日共提交 29 份缺陷报告（bug report），其中 9 份标记为 confirmed、12 份标记为 fixed、7 份处于等待回复状态、1 份标记为未被采纳。该快照按目标系统统计如下：

| GDBMS | Submitted | Confirmed (unfixed) | Fixed | Pending | Not adopted |
|---|---:|---:|---:|---:|---:|
| Neo4j | 7 | 5 | 1 | 0 | 1 |
| ArcadeDB | 6 | 0 | 6 | 0 | 0 |
| LadybugDB | 7 | 2 | 5 | 0 | 0 |
| Memgraph | 5 | 1 | 0 | 4 | 0 |
| Apache AGE | 3 | 1 | 0 | 2 | 0 |
| FalkorDB | 1 | 0 | 0 | 1 | 0 |
| **合计** | **29** | **9** | **12** | **7** | **1** |

按缺陷类型统计如下：

| 缺陷类型 | Submitted | Confirmed (unfixed) | Fixed | Pending | Not adopted |
|---|---:|---:|---:|---:|---:|
| 性能缺陷 | 25 | 8 | 9 | 7 | 1 |
| 逻辑缺陷 | 4 | 1 | 3 | 0 | 0 |
| **合计** | **29** | **9** | **12** | **7** | **1** |

其中，25 份性能缺陷报告用于评价性能预言和候选确认的实际有效性；4 份逻辑缺陷报告源于结果等价验证中观察到的结果不一致，属于验证阶段的伴随发现，不计入性能缺陷候选产出。

基于该项目状态快照，可计算以下描述性指标：

\[
\begin{aligned}
\operatorname{ConfirmationRate}_{\mathrm{track}}
&=
\frac{N_{\mathrm{confirmed}}^{\mathrm{track}}+N_{\mathrm{fixed}}^{\mathrm{track}}}
{N_{\mathrm{submitted}}^{\mathrm{track}}}
=
\frac{21}{29}
=72.4\%,\\
\operatorname{FixRate}_{\mathrm{track}}
&=
\frac{N_{\mathrm{fixed}}^{\mathrm{track}}}{N_{\mathrm{submitted}}^{\mathrm{track}}}
=
\frac{12}{29}
=41.4\%.
\end{aligned}
\]

上述指标以缺陷报告为统计单位，不等同于去重后的独立根因数量。下标 \(\mathrm{track}\) 表示计算依据为项目状态标注，而非经公开证据审计后的最终状态。

#### 3.4.3 公开证据审计

对 29 个公开 issue 的当前状态进行逐项核验后，项目状态标签与公开证据之间存在差异。公开记录当前支持以下分类：

| 公开证据状态 | 数量 | 代表性情况 |
|---|---:|---|
| 有公开修复证据 | 12 | 合并 PR、关联修复提交或维护者给出修复版本 |
| 明确确认但尚未修复 | 1 | Neo4j #13866 被维护者明确称为真实缺陷 |
| 已标记为 bug 或进入调查，但尚未明确确认 | 3 | Neo4j #13864、#13865、#13867 |
| 维护者要求基于候选修复复测，公开复测结论缺失 | 2 | LadybugDB #668、#669 |
| 作为优化请求或 feature 处理 | 1 | Memgraph #4208 |
| 等待回复 | 7 | Memgraph #4456–#4459、Apache AGE #2480–#2481、FalkorDB #2081 |
| 判定为预期行为或不计划处理 | 2 | Neo4j #13813、#13814 |
| 重新归因于上游系统 | 1 | Apache AGE #2428 |
| **合计** | **29** |  |

严格公开证据口径下的类型分布如下：

| 缺陷类型 | Submitted | Confirmed (unfixed) | Fixed | Confirmed or fixed |
|---|---:|---:|---:|---:|
| 性能缺陷 | 25 | 0 | 9 | 9 |
| 逻辑缺陷 | 4 | 1 | 3 | 4 |
| **合计** | **29** | **1** | **12** | **13** |

因此，\(N_{\mathrm{confirmed}}^{\mathrm{track}}=9\) 和 \(N_{\mathrm{fixed}}^{\mathrm{track}}=12\) 构成项目状态快照；最终有效性统计中的开发者确认数量以状态冻结时的公开证据审计结果为准。完整 issue 清单、项目状态与公开证据状态见第 4.2 节。

在第 3.4.1 节的严格判定条件下，公开证据支持 1 个已确认但未修复报告和 12 个已修复报告。相应的外部验证指标为：

\[
\begin{aligned}
\operatorname{ConfirmationRate}_{\mathrm{evidence}}
&=
\frac{N_{\mathrm{confirmed}}^{\mathrm{evidence}}+N_{\mathrm{fixed}}^{\mathrm{evidence}}}
{N_{\mathrm{submitted}}^{\mathrm{evidence}}}
=\frac{1+12}{29}
=44.8\%,\\
\operatorname{FixRate}_{\mathrm{evidence}}
&=
\frac{N_{\mathrm{fixed}}^{\mathrm{evidence}}}
{N_{\mathrm{submitted}}^{\mathrm{evidence}}}
=\frac{12}{29}
=41.4\%.
\end{aligned}
\]

其中，已修复报告同时构成开发者正向验证证据，但在互斥状态分布中不重复计入 confirmed。该口径将“报告已获开发者确认或修复”与“尚在调查、等待复测或被重新归因”区分开。

对性能缺陷报告单独计算，公开证据支持的确认率和修复率均为 \(9/25=36.0\%\)。逻辑缺陷报告的相应比例仅作为结果等价验证的伴随证据报告，不与性能缺陷报告合并解释为性能测试产出。

#### 3.4.4 独立复验与候选确认

候选验证与报告流程定义为：

\[
\begin{aligned}
N_{\mathrm{candidate}}
&\rightarrow N_{\mathrm{reproduced}}
\rightarrow N_{\mathrm{reduced}}\\
&\rightarrow N_{\mathrm{submitted}}
\rightarrow
\left\{
N_{\mathrm{confirmed}},
N_{\mathrm{fixed}},
N_{\mathrm{pending}},
N_{\mathrm{not\_adopted}}
\right\}.
\end{aligned}
\]

独立复验过程采用以下判定条件：

- 使用两个不同会话或进程；
- 核对图快照和 GDBMS 指纹；
- 在每个会话重新实施完整类型结果等价检查；
- 采用平衡随机配对测量；
- 要求慢侧方向一致；
- 同时检查保守倍率、区间下界和绝对时间差门槛。

独立复验目前属于候选形成后的离线步骤，尚未纳入当前候选发现主流程。只有完成该步骤并保存相应测试制品的候选才计入 \(N_{\mathrm{reproduced}}\)。

#### 3.4.5 症状保持约简

症状保持约简依据顶层子句和近似变量依赖提出删除方案，并通过外部症状判据重新执行查询对。只有在完整结果等价和性能现象保持时才接受删除。

该过程与差分调试的症状保持简化思想相关 [14]，但现有过程不提供完整抽象语法树（Abstract Syntax Tree，AST）成对约简或局部 1-最小性保证。其方法能力界定为“依赖约束的症状保持约简”，不包含最小查询保证。

候选复验率和约简率定义为：

\[
\operatorname{ReproductionRate}
=
\frac{N_{\mathrm{reproduced}}}{N_{\mathrm{candidate}}},
\qquad
\operatorname{ReductionRate}
=
\frac{N_{\mathrm{reduced}}}{N_{\mathrm{reproduced}}}.
\]

#### 3.4.6 开发者反馈与统计边界

- 开发者确认或修复属于案例级外部证据；
- 重复、暂不处理、无法复现、预期行为、上游归因、已确认和已修复分别统计；
- 未回复样本列为 pending，不计为假阳性，也不进入已裁决报告的分母；
- 开发者反馈不反向进入查询生成、查询重写或性能预言；
- 报告数量属于 report-level effectiveness，不替代对方法覆盖率、等价查询验证行为和候选稳定性的独立分析。

### 3.5 研究问题与方法主张的对应关系

| 研究问题 | 主要验证对象 | 不能单独支持的结论 |
|---|---|---|
| RQ1 | 测试输入生成、等价查询重写的适用范围及经验证查询对产出 | 通用 Cypher 等价、完整语言覆盖 |
| RQ2 | 规则前置条件检查与结果等价验证的作用及跨系统差异 | 完备假阳性率、所有结果不一致的根因 |
| RQ3 | 随机化配对执行相对于固定顺序的稳定性作用 | 完全消除系统噪声或顺序效应 |
| RQ4 | 候选复验率、症状保持约简率，以及缺陷报告的 submitted、confirmed 与 fixed 分布 | 报告数量等同于独立根因数量、未回复等同于假阳性 |

---


## 4. 参考文献与公开案例材料

### 4.1 方法相关参考文献

1. Ziyue Hua, Wei Lin, Luyao Ren, Zongyang Li, Lu Zhang, Wenpin Jiao, and Tao Xie. 2023. *GDsmith: Detecting Bugs in Cypher Graph Database Engines*. Proceedings of the 32nd ACM SIGSOFT International Symposium on Software Testing and Analysis, 163–174. [https://doi.org/10.1145/3597926.3598046](https://doi.org/10.1145/3597926.3598046).

2. Qiuyang Mang, Aoyang Fang, Boxi Yu, Hanfei Chen, and Pinjia He. 2024. *Testing Graph Database Systems via Equivalent Query Rewriting*. Proceedings of the IEEE/ACM 46th International Conference on Software Engineering, Article 143, 143:1–143:12. [https://doi.org/10.1145/3597503.3639200](https://doi.org/10.1145/3597503.3639200).

3. Zeyang Zhuang, Penghui Li, Pingchuan Ma, Wei Meng, and Shuai Wang. 2023. *Testing Graph Database Systems via Graph-Aware Metamorphic Relations*. Proceedings of the VLDB Endowment 17(4), 836–848. [https://doi.org/10.14778/3636218.3636236](https://doi.org/10.14778/3636218.3636236).

4. Tsong Yueh Chen, Fei-Ching Kuo, Huai Liu, Pak-Lok Poon, Dave Towey, T. H. Tse, and Zhi Quan Zhou. 2018. *Metamorphic Testing: A Review of Challenges and Opportunities*. ACM Computing Surveys 51(1), Article 4, 4:1–4:27. [https://doi.org/10.1145/3143561](https://doi.org/10.1145/3143561).

5. Celine Wüst, Zuming Jiang, and Zhendong Su. 2026. *Dinkel: State-Aware and Granular Framework for Validating Graph Databases*. Proceedings of the VLDB Endowment 19(6), 1198–1211. [https://doi.org/10.14778/3797919.3797928](https://doi.org/10.14778/3797919.3797928).

6. Lei Tang, Wensheng Dou, Yingying Zheng, Lijie Xu, Wei Wang, Jun Wei, and Tao Huang. 2025. *Proving Cypher Query Equivalence*. Proceedings of the 41st IEEE International Conference on Data Engineering, 3931–3944. [https://doi.org/10.1109/ICDE65448.2025.00293](https://doi.org/10.1109/ICDE65448.2025.00293).

7. Nadime Francis, Alastair Green, Paolo Guagliardo, Leonid Libkin, Tobias Lindaaker, Victor Marsault, Stefan Plantikow, Mats Rydberg, Petra Selmer, and Andrés Taylor. 2018. *Cypher: An Evolving Query Language for Property Graphs*. Proceedings of the 2018 International Conference on Management of Data, 1433–1445. [https://doi.org/10.1145/3183713.3190657](https://doi.org/10.1145/3183713.3190657).

8. Manuel Rigger and Zhendong Su. 2020. *Finding Bugs in Database Systems via Query Partitioning*. Proceedings of the ACM on Programming Languages 4(OOPSLA), Article 211, 211:1–211:30. [https://doi.org/10.1145/3428279](https://doi.org/10.1145/3428279).

9. Matteo Kamm, Manuel Rigger, Chengyu Zhang, and Zhendong Su. 2023. *Testing Graph Database Engines via Query Partitioning*. Proceedings of the 32nd ACM SIGSOFT International Symposium on Software Testing and Analysis, 140–149. [https://doi.org/10.1145/3597926.3598044](https://doi.org/10.1145/3597926.3598044).

10. Xinyu Liu, Qi Zhou, Joy Arulraj, and Alessandro Orso. 2022. *Automatic Detection of Performance Bugs in Database Systems Using Equivalent Queries*. Proceedings of the 44th International Conference on Software Engineering, 225–236. [https://doi.org/10.1145/3510003.3510093](https://doi.org/10.1145/3510003.3510093).

11. Andy Georges, Dries Buytaert, and Lieven Eeckhout. 2007. *Statistically Rigorous Java Performance Evaluation*. Proceedings of the 22nd ACM SIGPLAN Conference on Object-Oriented Programming Systems and Applications, 57–76. [https://doi.org/10.1145/1297105.1297033](https://doi.org/10.1145/1297105.1297033).

12. Christoph Laaber, Joel Scheuner, and Philipp Leitner. 2019. *Software Microbenchmarking in the Cloud. How Bad Is It Really?* Empirical Software Engineering 24(4), 2469–2508. [https://doi.org/10.1007/s10664-019-09681-1](https://doi.org/10.1007/s10664-019-09681-1).

13. Jinho Jung, Hong Hu, Joy Arulraj, Taesoo Kim, and Woonhak Kang. 2019. *APOLLO: Automatic Detection and Diagnosis of Performance Regressions in Database Systems*. Proceedings of the VLDB Endowment 13(1), 57–70. [https://doi.org/10.14778/3357377.3357382](https://doi.org/10.14778/3357377.3357382).

14. Andreas Zeller and Ralf Hildebrandt. 2002. *Simplifying and Isolating Failure-Inducing Input*. IEEE Transactions on Software Engineering 28(2), 183–200. [https://doi.org/10.1109/32.988498](https://doi.org/10.1109/32.988498).

### 4.2 缺陷报告数据集

表中汇总了截至 2026 年 7 月 29 日提交的 29 份公开缺陷报告。“项目状态”保留项目跟踪记录中的人工标注，用于复现第 3.4.2 节的描述性统计；“公开证据核验结果”依据 issue、关联 pull request 和修复提交中的可核查记录重新分类。两类状态分别报告，以保留原始统计口径并揭示外部证据的不确定性。

| GDBMS | Bug report | 类型 | 项目状态 | 公开证据核验结果 |
|---|---|---|---|---|
| Neo4j | [#13813](https://github.com/neo4j/neo4j/issues/13813) [I4] | 性能 | Confirmed | 维护者将该行为解释为确定性规划策略的预期结果，并表示维持现有行为。 |
| Neo4j | [#13864](https://github.com/neo4j/neo4j/issues/13864) | 性能 | Confirmed | issue 带有 bug 标签；维护者表示将进行调查，尚无明确确认或修复证据。 |
| Neo4j | [#13865](https://github.com/neo4j/neo4j/issues/13865) | 性能 | Confirmed | issue 带有 bug 标签；维护者将是否实施短路优化交由规划器团队评估，尚无明确确认或修复证据。 |
| Neo4j | [#13866](https://github.com/neo4j/neo4j/issues/13866) | 逻辑 | Confirmed | 维护者明确判定该现象为真实缺陷，尚无公开修复证据。 |
| Neo4j | [#13867](https://github.com/neo4j/neo4j/issues/13867) | 性能 | Confirmed | issue 带有 bug 标签并进入规划器团队调查，尚无明确确认或修复证据。 |
| Neo4j | [#13783](https://github.com/neo4j/neo4j/issues/13783) | 性能 | Fixed | 维护者说明相关表达式自 2026.07 版本起得到化简。 |
| Neo4j | [#13814](https://github.com/neo4j/neo4j/issues/13814) | 性能 | Not adopted | 维护者认为该模式较少见，并表示暂无实现相应化简的计划。 |
| ArcadeDB | [#5116](https://github.com/ArcadeData/arcadedb/issues/5116) | 性能 | Fixed | 维护者明确说明修复已进入主分支，并关联修复提交。 |
| ArcadeDB | [#5117](https://github.com/ArcadeData/arcadedb/issues/5117) | 性能 | Fixed | 维护者明确说明修复已进入主分支及 26.7.2 版本，并关联修复提交。 |
| ArcadeDB | [#5363](https://github.com/ArcadeData/arcadedb/issues/5363) | 性能 | Fixed | 维护者确认问题并说明修复已进入主分支，同时关联修复提交。 |
| ArcadeDB | [#4405](https://github.com/ArcadeData/arcadedb/issues/4405) [I2] | 性能 | Fixed | 修复由已合并的 [PR #4412](https://github.com/ArcadeData/arcadedb/pull/4412) 提供。 |
| ArcadeDB | [#5360](https://github.com/ArcadeData/arcadedb/issues/5360) | 逻辑 | Fixed | 维护者确认问题并说明修复已进入主分支，同时关联修复提交。 |
| ArcadeDB | [#5362](https://github.com/ArcadeData/arcadedb/issues/5362) | 性能 | Fixed | 维护者确认问题并说明修复已进入主分支，同时关联修复提交。 |
| LadybugDB | [#696](https://github.com/LadybugDB/ladybug/issues/696) [I3] | 逻辑 | Fixed | 修复由已合并的 [PR #702](https://github.com/LadybugDB/ladybug/pull/702) 提供。 |
| LadybugDB | [#720](https://github.com/LadybugDB/ladybug/issues/720) | 性能 | Fixed | 修复由已合并的 [PR #726](https://github.com/LadybugDB/ladybug/pull/726) 提供。 |
| LadybugDB | [#721](https://github.com/LadybugDB/ladybug/issues/721) | 性能 | Fixed | 修复由已合并的 [PR #726](https://github.com/LadybugDB/ladybug/pull/726) 提供。 |
| LadybugDB | [#668](https://github.com/LadybugDB/ladybug/issues/668) | 性能 | Confirmed | 维护者指向已合并的 [PR #671](https://github.com/LadybugDB/ladybug/pull/671) 并要求复测；公开记录中缺少复测结论。 |
| LadybugDB | [#669](https://github.com/LadybugDB/ladybug/issues/669) [I1] | 性能 | Confirmed | 维护者指向已合并的 [PR #671](https://github.com/LadybugDB/ladybug/pull/671) 并要求复测；公开记录中缺少复测结论。 |
| LadybugDB | [#697](https://github.com/LadybugDB/ladybug/issues/697) | 逻辑 | Fixed | 修复由已合并的 [PR #701](https://github.com/LadybugDB/ladybug/pull/701) 提供。 |
| LadybugDB | [#698](https://github.com/LadybugDB/ladybug/issues/698) | 性能 | Fixed | 修复由已合并的 [PR #703](https://github.com/LadybugDB/ladybug/pull/703) 提供。 |
| Memgraph | [#4456](https://github.com/memgraph/memgraph/issues/4456) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Memgraph | [#4457](https://github.com/memgraph/memgraph/issues/4457) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Memgraph | [#4458](https://github.com/memgraph/memgraph/issues/4458) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Memgraph | [#4208](https://github.com/memgraph/memgraph/issues/4208) | 性能 | Confirmed | 维护者将该现象作为功能或缺失优化处理，并说明其优先级低于其他功能。 |
| Memgraph | [#4459](https://github.com/memgraph/memgraph/issues/4459) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Apache AGE | [#2480](https://github.com/apache/age/issues/2480) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Apache AGE | [#2481](https://github.com/apache/age/issues/2481) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |
| Apache AGE | [#2428](https://github.com/apache/age/issues/2428) [I5] | 性能 | Confirmed | 维护者复现实验后将现象归因于 PostgreSQL 规划器的通用表达式化简限制，而非 Apache AGE 特有缺陷。 |
| FalkorDB | [#2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | 性能 | Pending | 截止状态冻结日期无实质性维护者回复。 |

公开证据审计表明，项目状态中的 confirmed 不等价于第 3.4.1 节定义的开发者确认。Neo4j #13813 被判定为预期行为，Apache AGE #2428 被重新归因于上游规划器，LadybugDB #668 和 #669 则缺少候选修复后的公开复测结论。有效性结果因而同时报告原始项目状态分布和公开证据审计分布，不将标签、关闭状态或缺少回复单独解释为缺陷成立或缺陷不成立。

# GraphSymPerf：基于图自同构轨道的 GDBMS 性能一致性测试

> 文档性质：方法研究大纲。本文定义的是待实现、待实验验证的目标方法，不代表当前仓库主实验路径已经具备这些能力。

## 1. 核心结论

GraphSymPerf 不再从查询文本出发寻找一般性的语义保持变换，而是从图数据库管理系统（Graph Database Management System，GDBMS）的数据模型出发构造测试预言：

> 若干带根节点标记的标签属性图在结构上同构，且同一查询模板在这些图实例上的可观察结果可由已知同构映射一一对应，那么在控制物理装载、缓存和测量噪声后，它们不应持续出现数量级运行时间差异。

方法的基本测试单位不是查询对，而是一个**对称测试族**：

\[
\mathcal{F}
=
\left(
\{G_1,\ldots,G_m\},
Q,
\{h_{ij}\},
\mathcal{C},
\mathcal{P}
\right),
\]

其中：

- \(G_i\) 是带根节点标记的标签属性图实例；
- \(Q\) 是在所有实例上保持不变的参数化查询模板；
- \(h_{ij}:G_i\to G_j\) 是生成器给出的结构保持同构；
- \(\mathcal{C}\) 是图结构与结果映射证书；
- \(\mathcal{P}\) 是随机装载、重复执行和噪声校准协议。

这个定义把研究对象限定为**同构图状态上的性能不对称**，而不是一般查询优化、任意等价查询之间的耗时差异或单条慢查询。

---

## 2. 为什么从图自同构讲故事

### 2.1 图数据库中的结构不应依赖顶点名字

标签属性图（Labeled Property Graph，LPG）由节点、关系、方向、标签、关系类型和属性组成。图模式查询的语义取决于这些结构及约束，而不应取决于节点在文件中的行号、插入次序或内部编号。

由属性图模式的结构匹配定义可以推出一种结构协变性（equivariance）：若 \(h:G_i\to G_j\) 保持查询可观察的图结构，则查询结果也应沿 \(h\) 映射 [4]。对于计数、存在性和其他同构不变量，两个实例上的结果应完全相同；对于返回节点、边或路径的查询，结果应在应用 \(h\) 后相同。

### 2.2 自同构轨道提供可控的“相同逻辑工作”

给定未加根标记的图 \(G\)，其自同构群记为：

\[
\operatorname{Aut}_{\Sigma}(G)
=
\{\gamma:V\to V\mid \gamma \text{ 保持 } \Sigma \text{ 中声明的结构与颜色}\}.
\]

\(\Sigma\) 至少包含：

- 节点标签；
- 边类型与方向；
- 多重边身份和自环；
- 查询会读取的属性；
- 查询会使用的索引与约束角色。

节点 \(v\) 的轨道为：

\[
\operatorname{Orb}(v)
=
\{\gamma(v)\mid \gamma\in\operatorname{Aut}_{\Sigma}(G)\}.
\]

同一轨道中的节点在声明的结构维度上不可区分。GraphSymPerf 选择一个非平凡轨道
\(O=\{v_1,\ldots,v_m\}\)，依次把相同的根标记附着到各 \(v_i\)，得到一组根化图：

\[
G_i=\operatorname{Root}(G,v_i).
\]

若 \(v_i\) 与 \(v_j\) 位于同一轨道，则存在 \(h_{ij}\) 使 \(G_i\cong G_j\)。这样可以在**保持查询文本不变**的前提下，为同一查询构造多个结构同构的测试输入。

### 2.3 为什么不能直接使用普通随机图

随机生成不能保证存在可用对称性。Erdős 与 Rényi 的经典工作表明，图规模增大时，绝大多数普通随机图是非对称图，即其自同构群只有恒等映射 [1]。

因此，本方法需要专门的**对称性保持图生成器**。如果仍使用无约束随机图生成，再在生成后寻找非平凡轨道，方法会产生大量“无可测试轨道”的输入，覆盖率和统计效力均不可控。

---

## 3. 研究问题与可证伪假设

### 3.1 研究问题

本文关注以下问题：

> GDBMS 是否会因为同构图实例的物理命名、装载次序、根节点位置或内部表示差异，对逻辑上结构相同的图模式查询产生稳定且显著的性能偏置？

这类偏置可能来自：

- 基数估计对内部标识或采样位置敏感；
- 邻接表、方向索引或标签索引实现不对称；
- 图模式匹配的起始点和扩展顺序选择不稳定；
- 物理装载顺序泄漏到逻辑计划选择；
- 缓存、分区或存储布局放大了本应可交换的结构角色；
- 对同构局部结构生成了规模明显不同的中间结果。

图模式的匹配顺序能够造成显著性能差异，模式自身的对称性也会引入大量重复计算；这些现象已经在图模式匹配系统研究中被明确观察 [2,3]。图查询优化又高度依赖基数估计，而属性图查询的方向、标签、类型和模式拓扑会直接影响匹配与估计 [4-6]。这些工作说明“结构角色相同但执行代价失衡”具有现实机制基础，但不直接证明任意同构实例都必须耗时完全一致。

### 3.2 语义关系：应当成立的正确性条件

给定 \(h_{ij}:G_i\to G_j\) 和固定查询模板 \(Q\)，定义查询观察：

\[
\operatorname{Obs}(Q,G_i)
=
(\text{schema},\text{typed rows},\text{multiplicity},\text{order status},\text{exception status}).
\]

语义关系为：

\[
h_{ij*}\bigl(\operatorname{Obs}(Q,G_i)\bigr)
=
\operatorname{Obs}(Q,G_j),
\]

其中 \(h_{ij*}\) 表示把结果中的节点、边和路径身份按同构映射转换。若 \(Q\) 返回的是同构不变量，则退化为：

\[
\operatorname{Obs}(Q,G_i)
=
\operatorname{Obs}(Q,G_j).
\]

结果不满足该关系时，测试族退出性能比较，转入逻辑异常或生成器错误调查。

### 3.3 性能关系：需要实验检验的假设

运行时间不是图查询语言语义的一部分，因此不能把性能相等写成定理。本文只提出可证伪的工程假设：

\[
T(Q,G_i)
\overset{d}{\approx}
T(Q,G_j),
\]

其中 \(\overset{d}{\approx}\) 表示在相同系统版本、资源状态、缓存模式和交叉装载协议下，运行时间分布应处于目标系统的正常噪声范围。

若结果映射成立，但某一轨道成员在多次会话、多个物理副本和随机执行顺序下持续显著变慢，则将其标为：

> **轨道性能不对称候选（orbit-performance-asymmetry candidate）**

该标签只表示可重复现象，不自动等价于已确认性能缺陷；根因归因仍需计划、算子计数、源码或开发者反馈等独立证据。

---

## 4. 对图结构生成的硬性要求

### 4.1 非平凡、可认证的轨道

每个测试族必须至少包含一个大小不小于 2 的轨道。为了支持离群点检测，正式实验优先生成大小不小于 4 的轨道。

生成器不能只报告“两个局部结构看起来相似”，而必须输出：

- 自同构生成元或显式映射；
- 轨道划分；
- 规范标号哈希；
- 被映射节点、边、标签、类型和属性的对应关系；
- 根化后各实例的同构证书。

可以把有向多重属性图编码为带颜色的简单图，再使用规范标号和自同构群计算工具验证；nauty/Traces 等工具为规范标号与自同构群计算提供了成熟实现基础 [10]。

### 4.2 方向、标签、关系类型和多重性必须被保持

对于 LPG，同构不能只保持无标签邻接关系。严格模式至少要求：

\[
(u,e,v)\in E
\iff
\bigl(h(u),h(e),h(v)\bigr)\in E,
\]

并保持：

- 边方向；
- 节点标签集合；
- 边类型；
- 查询可见属性；
- 平行边数量；
- 自环；
- 路径长度与关系唯一性约束。

属性图模式本身可形式化为有向、带标签的多重图，匹配时是否保持关系方向也是显式语义参数 [4]。因此，“交换边两端”不能被默认视为对称；只有生成器给出保持方向和类型的映射时才成立。

### 4.3 根选择器不能污染被测试结构

实际查询需要定位根节点。若直接为轨道成员写入不同的唯一属性值，这些值会破坏严格属性保持。GraphSymPerf 采用两级方案：

1. **快速筛选：** 在同一图中使用统一类型、统一索引和相同选择性的测试专用选择器；该选择器只负责绑定根，不能出现在后续过滤、投影或聚合中。
2. **提升验证：** 为每个轨道成员分别构造一个根化图实例 \(G_i\)，所有实例使用相同的根标签和相同的选择器值。实例之间由 \(h_{ij}\) 保证整体同构，查询文本和参数完全不变。

正式报告必须通过第二级验证，避免把属性值、索引桶或参数差异误判为结构对称性违例。

### 4.4 轨道外背景也必须对称附着

只复制局部基本结构（motif）还不够。若某个轨道成员与背景图的连接方式不同，完整图上的自同构会被破坏。

背景附着遵循轨道提升（orbit lifting）原则：

- 对某一轨道成员增加一条背景边时，必须按群作用为整个轨道增加对应边；
- 标签、边类型、方向、属性分布和多重性同步复制；
- 对称分支的边界度数、入度、出度和 \(k\)-跳邻域哈希保持一致；
- 任一非对称扰动必须被标为对照组，而不能留在正式对称族中。

### 4.5 结构相同之外，还要保证查询工作量非退化

合法同构图不一定是有效性能测试输入。生成器还需要满足：

- 查询至少有一个匹配，避免全空结果主导；
- 结果规模不超过完整比较上限；
- 根绑定具有统一且可控的选择性；
- 查询会触发足够的边扩展、连接或路径枚举；
- 对称成员的真实结果基数完全相同；
- 轨道规模、分支因子、路径长度和背景密度可独立调节；
- 不把完整图扫描、超时或内存耗尽当作唯一触发机制。

gMark 说明图实例与查询工作负载应联合生成，并应显式控制形状、规模和选择性 [5]；G-CARE 的实验设计也表明，查询拓扑、大小和真实基数是评估图查询估计与执行行为时不可省略的维度 [6]。

### 4.6 必须同时生成负对照

每个对称测试族配套生成一个或多个最小破坏对照：

- 只给一个轨道成员增加一条边；
- 只改变一个边类型或方向；
- 只改变一个查询可见属性；
- 只改变一个分支的度数或路径长度。

破坏后，原同构证书必须失效。负对照用于验证：

1. 证书检查器能够拒绝伪对称输入；
2. 结果映射器不会把真实语义差异归一化掉；
3. 性能检测器不会对非同构输入继续使用轨道预言。

---

## 5. 对称图族的构造模板

### 5.1 重复分支

构造中心节点 \(c\) 与 \(m\) 个完全同构分支：

\[
c \rightarrow B_1,\ldots,c\rightarrow B_m.
\]

群 \(S_m\) 可置换各分支。每个分支根形成同一轨道。可调参数包括分支深度、扇出、标签选择性、关系类型和背景噪声。

适合压力点：

- 邻接扩展；
- 标签与属性索引；
- 分支基数估计；
- 多跳模式连接。

### 5.2 环与二面体对称

构造带一致颜色的环 \(C_k\)。旋转与反射形成二面体群 \(D_k\)。根标记依次放置到环上各轨道成员，固定查询模板执行定长或有界路径匹配。

适合压力点：

- 路径起点选择；
- 双向邻接访问；
- 环检测；
- 有界路径枚举。

若边有方向，则必须使用方向保持的有向环；不能用反射映射隐式反转边方向。

### 5.3 成对镜像的有向基本结构

构造两个结构同构、方向和类型均严格对应的有向子图，并以相同方式连接背景。映射只在两个子图之间交换对应节点和边，不改变单条边的语义。

适合压力点：

- 入邻接与出邻接实现；
- 关系类型索引；
- 方向敏感的基数统计；
- 多关系连接。

### 5.4 同构组件嵌入现实背景

为降低纯规则图与现实图分布差异，可将多个同构组件嵌入采样得到的现实背景，但组件与背景的接口必须按轨道整体复制。

该模式只能支持以下论断：

> 对称组件被嵌入具有现实分布特征的背景后，方法仍能构造有效且可重复的性能测试族。

它不能证明完全对称图在生产工作负载中高频出现。

---

## 6. 固定查询模板生成

### 6.1 不从查询文本做变换

查询生成器以 \((G,O,\Sigma)\) 为输入，直接生成一个根化查询模板 \(Q(x)\)。所有测试实例共享相同的：

- 查询文本；
- 参数名与参数类型；
- 返回列模式；
- 路径长度约束；
- 谓词位置；
- 投影与聚合；
- 索引提示策略。

测试族成员之间只改变根化图实例；正式提升阶段连查询参数值也保持不变。

### 6.2 查询模板约束

第一阶段只纳入：

- 只读、确定性查询；
- 根节点可唯一绑定；
- 不读取测试专用选择器；
- 不含无稳定排序的 `LIMIT`、`SKIP` 或顺序敏感聚合；
- 不含随机、时间、外部状态或副作用函数；
- 不把执行计划相同作为语义前提；
- 路径返回语义能够被映射器完整处理。

查询形状包括：

- 单跳和多跳根化模式；
- 星形、链形、环形和星链混合模式；
- 有界可变长度路径；
- 多分支共享根模式；
- 计数、存在性和其他同构不变量聚合；
- 返回节点、边或路径的映射敏感查询。

### 6.3 优先使用结果不变量查询

方法验证早期优先使用：

```cypher
MATCH p = (root:Anchor)-[:R*1..k]->(v)
RETURN count(p)
```

或：

```cypher
MATCH (root:Anchor)-[:R]->(v)-[:S]->(w)
RETURN count(*)
```

计数和存在性结果不需要暴露系统内部节点标识，能够简化跨同构实例比较。节点、边和路径返回查询在第二阶段加入，用于扩大语义覆盖。

---

## 7. 对称性证书

每个测试族必须保存机器可检验的证书：

```text
family_id
generator_seed
schema_signature
base_graph_hash
rooted_graph_hashes
automorphism_generators
orbit_partition
root_orbit
instance_isomorphisms
preserved_node_labels
preserved_edge_types
preserved_directions
preserved_properties
selector_policy
query_template_hash
result_mapping_policy
negative_control_mutations
```

证书分三层验证：

1. **生成时验证：** 构造器按已知群作用生成映射；
2. **离线独立验证：** 规范标号或 VF2 类算法重新检查根化图实例同构；
3. **加载后验证：** 从目标 GDBMS 导出必要图状态，核对节点数、边数、颜色类、轨道不变量和根标记。

任一层失败时，不执行性能预言。

---

## 8. 结果映射与正确性门禁

### 8.1 完整结果比较

性能测试前，完整执行 \(Q\) 并比较：

- 列数、列名和类型；
- 行多重性；
- 节点、边和路径的映射后身份；
- 标量、列表和嵌套值；
- 有显式 `ORDER BY` 时的顺序；
- 无显式顺序保证时的多重集；
- 异常类别、超时和截断状态。

只比较行数不足以确认同构关系。

### 8.2 路径结果

对路径 \(p=(v_0,e_1,\ldots,e_k,v_k)\)，映射结果为：

\[
h_*(p)
=
\bigl(h(v_0),h(e_1),\ldots,h(e_k),h(v_k)\bigr).
\]

若查询语义区分路径方向，则映射器必须保持路径序列，不能对路径随意反转或排序。

### 8.3 拒绝原因

至少记录：

- `NO_NONTRIVIAL_ORBIT`
- `ROOTED_GRAPH_NOT_ISOMORPHIC`
- `LOAD_STATE_CERTIFICATE_FAILED`
- `QUERY_NOT_DETERMINISTIC`
- `SELECTOR_LEAKED_INTO_RESULT`
- `RESULT_SCHEMA_MISMATCH`
- `RESULT_MAPPING_MISMATCH`
- `RESULT_TRUNCATED`
- `EXECUTION_ERROR`
- `MEASUREMENT_INCOMPLETE`

---

## 9. 物理化与测量协议

### 9.1 为什么同构证书仍不够

逻辑同构不保证物理布局相同。内部编号、页布局、邻接表顺序、分区、索引桶和缓存都可能受插入次序影响。若不控制这些因素，测到的可能只是一次装载偶然性。

因此，每个逻辑根化实例 \(G_i\) 生成多个物理副本：

\[
G_i^{(1)},G_i^{(2)},\ldots,G_i^{(p)},
\]

并随机化：

- 节点插入顺序；
- 边插入顺序；
- 非语义内部标识分配；
- 数据库重建次序；
- 查询族执行次序。

### 9.2 交叉装载

采用平衡交叉设计，使每个轨道角色在不同副本中经历不同物理位置。若性能异常只跟随某一次装载，而不跟随轨道成员或结构角色，则归类为物理化噪声，不提升为候选。

### 9.3 执行调度

每个会话：

1. 检查目标版本、资源状态和数据证书；
2. 执行固定数量预热；
3. 按随机平衡顺序运行各实例；
4. 插入相同查询的 A/A 噪声探针；
5. 记录完整执行、超时和连接状态；
6. 在独立会话或重连后复测；
7. 候选再跨物理副本复测。

热缓存和冷缓存必须分开报告，不能混合为一个总体时间。

---

## 10. 轨道级性能预言

### 10.1 基本统计量

设 \(M_i\) 为 \(Q\) 在 \(G_i\) 上多次完整执行时间的中位数，定义：

\[
R_{\mathrm{orbit}}
=
\frac{\max_i M_i}{\min_i M_i}.
\]

同时记录：

- 绝对时间差；
- 中位数绝对偏差（Median Absolute Deviation，MAD）；
- 配对比值的自助法置信区间；
- 每个物理副本中的慢向一致性；
- A/A 噪声分布的高分位数。

比例阈值只是筛选参数，不能单独证明缺陷。

### 10.2 分离结构效应与装载效应

对数时间可表示为：

\[
\log T_{i,p,r}
=
\mu+\alpha_i+\beta_p+(\alpha\beta)_{i,p}+\varepsilon_{i,p,r},
\]

其中：

- \(\alpha_i\) 是轨道成员效应；
- \(\beta_p\) 是物理副本效应；
- \((\alpha\beta)_{i,p}\) 是交互项；
- \(\varepsilon\) 是会话内噪声。

在同构假设下，不应出现跨物理副本稳定的大幅 \(\alpha_i\)。只有装载效应显著时，不归因于逻辑结构角色。

### 10.3 候选提升条件

候选至少满足：

1. 根化图实例的独立同构检查通过；
2. 完整类型结果在映射后相同；
3. 绝对时间差超过目标系统 A/A 噪声上界；
4. 比值置信区间下界超过预设筛选阈值；
5. 至少两个独立会话保持同一慢向；
6. 至少两个随机物理副本复现；
7. 负对照未被错误接受为对称测试族；
8. 缩减后仍保持同构证书、结果关系和性能现象。

### 10.4 执行计划只用于解释

计划不要求完全相同。不同计划可能同样合理，计划文本也可能因系统版本变化。

候选产生后再比较：

- 索引查找与标签扫描；
- 起始节点；
- 入边与出边扩展；
- 中间结果基数；
- 过滤下推；
- 路径算子；
- 内存、I/O 和缓存命中。

这些信息用于提出根因假设，不能替代运行时间预言，也不能在没有源码或开发者证据时写成已确认根因。

---

## 11. 方法流程

```text
Schema and Symmetry Specification
        ↓
Symmetry-Preserving Graph Generation
        ↓
Automorphism and Orbit Certification
        ↓
Rooted Isomorphic Instance Construction
        ↓
Fixed Query Template Generation
        ↓
Independent Isomorphism and Load-State Validation
        ↓
Mapping-Aware Complete Result Gate
        ↓
Randomized Cross-Physicalization Timing
        ↓
Orbit-Level Statistical Screening
        ↓
Plan Diagnosis and Symmetry-Preserving Reduction
        ↓
Revalidated Performance-Anomaly Candidate
```

对应模块：

1. `SymmetrySpec`：声明允许的群、颜色和结构不变量；
2. `SymmetricGraphGenerator`：生成基础图、轨道和负对照；
3. `AutomorphismCertifier`：计算规范标号、轨道和同构映射；
4. `RootedInstanceBuilder`：构造根化同构实例；
5. `AnchoredQueryGenerator`：为图族生成固定查询模板；
6. `MappedResultOracle`：执行映射感知的完整结果比较；
7. `PhysicalizationScheduler`：随机装载并实施交叉设计；
8. `OrbitPerformanceOracle`：执行噪声校准和轨道级统计；
9. `SymmetryPreservingReducer`：仅接受保持证书与现象的缩减。

---

## 12. 实验设计

### RQ1：能否稳定生成有效对称测试族？

指标：

- 非平凡轨道生成率；
- 平均轨道大小；
- 独立同构复核通过率；
- 加载后证书通过率；
- 固定查询可执行率；
- 完整结果门禁通过率。

### RQ2：专用图生成是否必要？

对比：

- 无约束随机图；
- 仅复制局部 motif、未认证的图；
- 带轨道证书的对称图；
- 对称组件嵌入现实背景的图。

重点验证普通随机图是否因自同构群平凡而缺少有效测试对象。

### RQ3：哪些图结构最容易触发性能不对称？

自变量：

- 轨道大小；
- motif 类型；
- 分支因子；
- 路径长度；
- 入度和出度；
- 标签与属性选择性；
- 图密度；
- 对称背景规模。

因变量：

- 轨道比值；
- 绝对时间差；
- 中间结果差；
- 复现率；
- 候选提升率。

### RQ4：物理装载会造成多少伪异常？

比较：

- 单次装载；
- 随机插入顺序；
- 多物理副本；
- 交叉装载；
- 热缓存与冷缓存。

报告有多少初筛异常在控制物理化后消失。

### RQ5：方法能否发现新的 GDBMS 性能缺陷？

报告链：

\[
\text{generated}
\rightarrow
\text{certified}
\rightarrow
\text{result-valid}
\rightarrow
\text{screened}
\rightarrow
\text{cross-load reproduced}
\rightarrow
\text{reduced}
\rightarrow
\text{reported}
\rightarrow
\text{developer-confirmed}.
\]

不能把筛选候选数直接写成缺陷数。

### 消融实验

- 去掉自同构证书；
- 去掉负对照；
- 去掉完整结果映射；
- 去掉 A/A 噪声校准；
- 去掉跨物理副本复测；
- 只使用计数查询；
- 只使用纯规则图；
- 不做对称保持缩减。

---

## 13. 与相关工作的边界

### 13.1 图模式对称性研究

GraphPi 指出，模式内部对称性会导致重复枚举，不同匹配顺序也可能产生巨大性能差异；其目标是通过破坏模式对称性和选择调度来加速图模式匹配 [3]。Fast Local Subgraph Counting 使用自同构轨道和对称性破坏减少子图计数成本 [2]。

GraphSymPerf 不设计新的图匹配执行算法，也不消除重复枚举。它把“同一轨道中的结构角色应可交换”转化为 GDBMS 的黑盒性能一致性检查。

### 13.2 图原生 GDBMS 测试

GAMERA 从节点、边、路径、连通性等图原生结构构造蜕变关系，主要检测结果不一致的逻辑缺陷 [7]。GraspDB 以标签属性图同构和子图同构为图状态持久性预言，主要检测写操作后的图状态错误 [9]。

GraphSymPerf 的对象是：

- 只读查询；
- 同构根化图实例；
- 映射后结果一致前提下的运行时间分布；
- 物理装载混杂控制后的性能离群。

因此，不能把“使用图同构”本身声称为首创；可能成立的差异点是**把自同构轨道、固定查询模板、根化同构实例和交叉物理化结合为性能预言**。

### 13.3 一般查询生成方法

GRev 从查询语义图生成能够触发不同计划的查询，并同时检查结果和运行时间 [8]。GraphSymPerf 不以生成不同查询表达为目标：正式验证阶段保持查询文本、参数和值不变，只改变由自同构轨道诱导的同构图状态。

当前定向检索尚未发现与上述完整流程相同的 GDBMS 性能测试方法，但这不是系统综述意义上的“首次”证明。投稿前仍需以 `graph automorphism/isomorphism + graph database + performance testing/metamorphic testing` 为核心检索式做前向、后向滚雪球和近年增量检索。

---

## 14. 当前实现边界与落地缺口

当前仓库存在查询表面结构变换模块，但它不等于本文定义的自同构轨道方法。现有主实验工作器也尚未接入以下关键能力：

- 对称性保持的图生成；
- LPG 颜色编码与自同构群计算；
- 轨道证书；
- 根化同构实例构造；
- 固定查询模板的跨实例执行；
- 映射感知结果比较；
- 多物理副本交叉装载；
- 轨道效应与装载效应分离；
- 保持同构证书的候选缩减。

因此，在上述模块实现并经测试前，论文中只能把本文件描述为**目标方法设计**，不能写成当前系统已完成能力，也不能用现有候选结果反向证明该方法有效。

建议实现顺序：

1. 先支持无向、无属性的重复分支和环；
2. 加入带颜色节点、边类型和方向；
3. 实现根化实例与计数查询；
4. 实现独立同构复核和负对照；
5. 接入完整结果门禁与现有运行时测量；
6. 加入多物理副本交叉装载；
7. 最后扩展路径返回、现实背景嵌入和对称保持缩减。

---

## 15. 主要风险

1. **性能不变量不是语言规范。** 即使同构图耗时不同，也只能先称为性能异常候选。
2. **纯对称图代表性有限。** 方法强调可控触发与归因，不代表生产图普遍高度对称。
3. **物理布局是最大混杂因素。** 不做跨装载复测，结论不可信。
4. **根选择器可能破坏对称性。** 正式验证必须使用根化同构实例和相同查询参数。
5. **属性图同构比无标签图复杂。** 标签、类型、方向、属性、多重边和路径语义遗漏任一项都可能制造误报。
6. **轨道计算存在成本。** 应由生成器保留构造映射，再用独立算法复核，而不是对任意大图盲算全部自同构。
7. **与 GAMERA、GraspDB 和 GRev 的边界必须写清。** 贡献不能停留在“我们也使用图原生关系或图同构”。

---

## 参考文献

1. P. Erdős and A. Rényi. 1963. *Asymmetric Graphs*. Acta Mathematica Academiae Scientiarum Hungaricae 14: 295–315. [PDF](https://users.renyi.hu/~p_erdos/1963-04.pdf).
2. Qiyan Li and Jeffrey Xu Yu. 2024. *Fast Local Subgraph Counting*. PVLDB 17(8): 1967–1980. [https://doi.org/10.14778/3659437.3659451](https://doi.org/10.14778/3659437.3659451).
3. Tianhui Shi, Mingshu Zhai, Yi Xu, and Jidong Zhai. 2020. *GraphPi: High Performance Graph Pattern Matching through Effective Redundancy Elimination*. SC 2020, Article 100, 1–14. [arXiv:2009.10955](https://arxiv.org/abs/2009.10955).
4. Leonard Wörteler, Moritz Renftle, Theodoros Chondrogiannis, and Michael Grossniklaus. 2022. *Cardinality Estimation using Label Probability Propagation for Subgraph Matching in Property Graph Databases*. EDBT 2022: 285–297. [https://doi.org/10.48786/EDBT.2022.16](https://doi.org/10.48786/EDBT.2022.16).
5. Guillaume Bagan, Angela Bonifati, Radu Ciucanu, George H. L. Fletcher, Aurélien Lemay, and Nicky Advokaat. 2017. *gMark: Schema-Driven Generation of Graphs and Queries*. IEEE TKDE 29(4): 856–869. [https://doi.org/10.1109/TKDE.2016.2633993](https://doi.org/10.1109/TKDE.2016.2633993).
6. Yeonsu Park, Seongyun Ko, Sourav S. Bhowmick, Kyoungmin Kim, Kijae Hong, and Wook-Shin Han. 2020. *G-CARE: A Framework for Performance Benchmarking of Cardinality Estimation Techniques for Subgraph Matching*. SIGMOD 2020: 1099–1114. [https://doi.org/10.1145/3318464.3389702](https://doi.org/10.1145/3318464.3389702).
7. Zeyang Zhuang, Penghui Li, Pingchuan Ma, Wei Meng, and Shuai Wang. 2023. *Testing Graph Database Systems via Graph-Aware Metamorphic Relations*. PVLDB 17(4): 836–848. [https://doi.org/10.14778/3636218.3636236](https://doi.org/10.14778/3636218.3636236).
8. Qiuyang Mang, Aoyang Fang, Boxi Yu, Hanfei Chen, and Pinjia He. 2024. *Testing Graph Database Systems via Equivalent Query Rewriting*. ICSE 2024, Article 143, 1–12. [https://doi.org/10.1145/3597503.3639200](https://doi.org/10.1145/3597503.3639200).
9. Shuang Liu, Junhao Lan, Xiaoning Du, Jiyuan Li, Wei Lu, Jiajun Jiang, and Xiaoyong Du. 2024. *Testing Graph Database Systems with Graph-State Persistence Oracle*. ISSTA 2024: 666–677. [https://doi.org/10.1145/3650212.3680311](https://doi.org/10.1145/3650212.3680311).
10. Brendan D. McKay and Adolfo Piperno. 2014. *Practical Graph Isomorphism, II*. Journal of Symbolic Computation 60: 94–112. [https://doi.org/10.1016/j.jsc.2013.09.003](https://doi.org/10.1016/j.jsc.2013.09.003).

# Gremlin 实测覆盖

以下至 ArcadeDB 章节之前的 JanusGraph／HugeGraph 数据保留 **2026-10-09** 的历史口径。**2026-10-18** 新增 ArcadeDB 接入和后续 JanusGraph／HugeGraph 的 P3、P13、P16 扩展分别见后续章节，各轮证据分开统计。

本轮实际接入 **JanusGraph 1.1.0** 与 **Apache HugeGraph 1.7.0**。JanusGraph 的 12 项 Pattern 共 36 个小图输入、HugeGraph 的 11 项 Pattern 共 33 个小图输入，均完成构图、成本检查、完整结果检查、配对执行和原生 PROFILE 采集。另有 **29 项 JanusGraph／Neo4j 语义对照**通过。上述是功能与语义证据，不能用小图结果宣称没有性能缺陷；扩规模性能统计单独记录。

## 固定版本与环境

| 目标 | 实际返回版本 | TinkerPop | 本轮存储 | ARM64 固定镜像 |
|---|---|---|---|---|
| JanusGraph | `JanusGraph/1.1.0` | 3.7.3 | BerkeleyJE，默认 Lucene 配置 | `janusgraph/janusgraph@sha256:fa85deeacd8ec7b25fbd264b2cd6d1f070c8beff854bd850356f9d4f107f33c7` |
| Apache HugeGraph | `ApacheHugeGraph/1.7.0` | 3.5.1 | RocksDB | `hugegraph/hugegraph@sha256:6c39dbc14c78d707a585c3635a6ddbe8968241ce28802c97b38fa100397bc63c` |

版本来自各轮 `run.json` 和实际服务器探测。镜像摘要为该发布标签的 ARM64 子 manifest；AMD64 主机应使用对应架构或同一固定多架构 index，不能强制套用 ARM64 摘要。官方兼容依据为 [JanusGraph 1.1.0](https://docs.janusgraph.org/changelog/) 和 [HugeGraph 1.7.0 的 TinkerPop 版本声明](https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/pom.xml)。HugeGraph Docker 镜像是项目提供的便利发行，不是 ASF 的正式二进制发行包。[官方部署说明](https://hugegraph.apache.org/docs/quickstart/hugegraph/hugegraph-server/)

## 查询与图状态

实现沿用骨架 → `GraphSpec` → 角色绑定 → 查询中间表示（IR）→ 语义保持变换 → 语言生成。`Src/gremlin.py` 从公共 IR 直接生成显式遍历，常量通过独立 `bindings` 传输。没有实现任意 Cypher／Gremlin／GQL 的统一文本解析器，也没有把原 Cypher 文本交给数据库自动翻译。已有受限 Cypher 解析器只用于旧输入和可选种子导入。

`GraphSpec` 仍描述同一实验图。两库使用原生 schema、索引和批次装载，不增加框架服务或插件系统。JanusGraph 将顶点属性设为 `SINGLE`、关系设为 `MULTI`，HugeGraph 通过关系序号保留平行边。每次构图检查节点／边数量、属性、决定条件及低成本索引状态。JanusGraph 要求标签限定 composite index 为 `ENABLED`；HugeGraph 要求标签限定 secondary index 为 `CREATED`，检查发生在计时之前。

## 2026-10-09 的支持范围（历史）

下表仅针对本轮内置模板、受控图状态和实际版本，不是完整 Gremlin 能力声明。

| Pattern | JanusGraph | HugeGraph | 限制说明 |
|---|---|---|---|
| P1、P2、P4、P5、P6、P8、P9、P10、P11、P14、P15 | 已执行 | 已执行 | 有限单跳匹配、相关 EXISTS／COUNT、布尔表达式、CASE、UNION ALL 等 |
| P12 | 已执行 | 不支持 | 当前图配方让同一关系类型连接多种端点标签；普通 HugeGraph edge label 不能沿用该配方 |
| P3 | 不支持 | 不支持 | 尚未实现关系不重复的可变长度路径生成 |
| P7 | 不支持 | 不支持 | 顶点为单标签模型，不能复用多标签合取与标签顺序机制 |
| P13 | 不支持 | 不支持 | 尚未实现 OPTIONAL 的 NULL 延展 |
| P16 | 不支持 | 不支持 | 尚未实现 ALL 列表区域机制 |
| S1–S10 | 未纳入本轮覆盖 | 未纳入本轮覆盖 | 不因部分表达式可生成，就声称整个模板机制已接入 |

编译或建图遇到限制时记录 `unsupported_feature`；目标生成器消掉了 A/B 差异时记录 `unsupported_transform`，均不计为有效阴性实验。包含浮点输出的 DISTINCT 保守拒绝，避免引擎 map 去重与 Cypher 数值相等语义不一致。`simplePath()` 会排除节点重复，不能作为 Cypher 关系不重复路径的直接替代；Gremlin `optional()` 的回退输入也不直接等同于 Cypher 的 NULL 延展。[TinkerPop 路径与 OPTIONAL 语义](https://tinkerpop.apache.org/docs/3.7.3/reference/)

## 结果语义与验证

结果列统一投影为普通标量 map，再还原逻辑列名，按结果多重集比较，保留重复行和布尔／数值类型。JanusGraph 的 GraphSON 3 类型封装及 HugeGraph 的 JSON map 分别解码；未知类型、字段缺失、截断或结果不完整均拒绝进入性能比较。执行器消费 `max_rows + 1` 行以识别超限，不把截断结果当作完整结果。

JanusGraph／HugeGraph 的常量不拼接进遍历文本。每个 A、B、reference 保存自己的 `query_bindings`；文本相同但参数不同仍是不同输入。NULL 比较保留三值逻辑，`NOT` 测试 FALSE 而不是简单把“未匹配 TRUE”当作 FALSE；缺失属性投影为 NULL。`gremlin_null_projection` 必须经过版本验证后显式启用，不能用配置覆盖已知语义不兼容。

JanusGraph 的 29 项对照覆盖缺失属性、NOT／OR、可空投影、CASE、COALESCE 的首个非空标量类型、布尔与数值区分、64 位整数边界、相关 COUNT／EXISTS 空结果、UNION ALL 重复行、无向自环和平行边、关系变量复用，以及内部遍历标签不捕获用户变量。每项同时与构造期望结果和 **Neo4j 2026.09.0** 的真实 Cypher 结果比较。HugeGraph 本轮包含 33 个完整 Pattern 输入及可空投影探测，尚不能将 JanusGraph 的 29 项独立反例覆盖归到 HugeGraph。

证据保存在：

- [JanusGraph 小图轮次：36 例](../results/gremlin-expansion-20261009/janusgraph/20261009T115903Z-4bd509/run.json)。
- [HugeGraph 小图轮次：33 例，P12 未执行](../results/gremlin-expansion-20261009/hugegraph/20261009T121253Z-ea3b04/run.json)。
- [JanusGraph／Neo4j 语义对照](../results/gremlin-expansion-20261009/semantics-janusgraph-neo4j/)。
- [HugeGraph 原生投影探测](../results/gremlin-expansion-20261009/hugegraph-native-probe.json)。

小图轮次使用较高的初筛阈值，目的为接入验证，不作为正式缺陷发现或 Pattern 贡献统计。初期探测失败与修复前证据另行保留，不合并进通过数量。

HugeGraph 的扩规模准备曾在 `g.E().hasLabel(...).count()` 上出现 `Big id max length is 16384`。这来自边标签索引查询文本被序列化为缓存键，而不是实验节点 ID 太长。现将计时前的结构数量检查改为全边扫描与标签筛选，保留原实验遍历和物理索引；修复前轮次保留并由 `EXCLUDED.json` 排除。[查询缓存键源码](https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/hugegraph-core/src/main/java/org/apache/hugegraph/backend/cache/CachedBackendStore.java)、[序列化长度限制](https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/hugegraph-core/src/main/java/org/apache/hugegraph/backend/serializer/BytesBuffer.java)

## 性能证据边界

两个串行扩规模轮次使用 `scale=100/1000`、查询种子 `17`、图状态种子 `0`、`complexity=0`，完成 **46 个有效案例**，全部保留原生 PROFILE，共 104 份。采用默认的 6 组初筛和 2 次独立重建确认；每次确认包含 20 组 AB/BA 和各 10 组 A/A、B/B。超时为 30 秒，结果上限为 10,000 行。

| 目标 | 有效案例 | 未通过初筛 | 确认未通过 | 性能观察 | 新登记遗漏优化 BUG |
|---|---:|---:|---:|---:|---:|
| JanusGraph | 24 | 20 | 2 | 2 | 0 |
| HugeGraph | 22 | 18 | 0 | 4 | 0 |

通过确认的具体观察如下。比值对应该案例表中列出的查询对；不同查询对不能直接解释成同一差异随规模变化。

| 目标 / Pattern | 规模 | 比较 | 中位时间比值 |
|---|---:|---|---:|
| JanusGraph / P12 | 100 | A/B | 16.65 |
| JanusGraph / P12 | 1000 | A/B | 190.15 |
| HugeGraph / P4 | 100 | A/ref | 3.08 |
| HugeGraph / P4 | 1000 | A/B | 13.80 |
| HugeGraph / P5 | 100 | A/ref | 3.17 |
| HugeGraph / P5 | 1000 | A/B | 13.02 |

原始轮次为 [JanusGraph 24 例](../results/gremlin-expansion-20261009/performance-janusgraph/20261009T121722Z-cb3765/)与 [HugeGraph 修复后 22 例](../results/gremlin-expansion-20261009/performance-hugegraph/20261009T124032Z-93a535/)。[分 Pattern 汇总](../results/gremlin-expansion-20261009/summary/summary.csv)只纳入这两个正式轮次；烟测、重放和修复前失败轮次均显式排除。`seconds_per_pattern=90` 在下一案例启动前检查，不中断已启动的完整测量；P12 的实际总用时约 557 秒，统计保留实际耗时，不能将该参数称为硬时间上限。

生成器显式决定起点和遍历顺序，A/B 比较标记为 `pair_kind: explicit_traversal`。改变遍历方向、起点或操作次序可能直接改变规定的工作量；**即使完成独立重建、AB/BA、A/A、B/B 并通过差异阈值，也只记录 `timing_observation`，不会自动成为 `candidate` 或遗漏优化 BUG**。汇总的 `timing_observations` 单列，不并入对称／参考候选数量。后续只有证明目标引擎适用的优化规则遗漏了可避免工作，才可人工登记到 `bugs.csv` 和 `BUG list`。

普通查询计时包含持久 HTTP 连接上的请求、完整结果接收及解码；连接建立不计入。PROFILE 在普通计时结束后单独采集。原生 `TraversalMetrics`、step counts、traverser counts、duration 与 provider 嵌套指标保留原始格式和单位，**不能统称 Neo4j DB Hits**。Count 包含 traverser bulk，Traversers 表示实际遍历对象数量；PROFILE 本身有额外开销。[TinkerPop PROFILE 文档](https://tinkerpop.apache.org/docs/3.7.3/reference/#profile-step)

服务器超时和网络超时均停止当前目标，并记录 `cancellation_verified: false`；不自动继续下一案例，也不以客户端连接关闭宣称服务器查询已终止。当前实现不提供原生查询取消或超时候选确认。服务端 `evaluationTimeout` 的配置与客户端超时是两回事；调整实验超时必须同时检查服务器配置。[JanusGraph 服务配置](https://docs.janusgraph.org/operations/server/)、[HugeGraph 1.7.0 服务配置](https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/hugegraph-dist/src/assembly/static/conf/gremlin-server.yaml)

## 最简运行与重放

无需新增 Gremlin Python 驱动依赖。先按 README 安装仓库依赖，启动**仅用于本实验**的实例；下面使用固定多架构镜像 index，适合 ARM64／AMD64 主机：

```bash
docker run -d --name gsp-janusgraph \
  -p 127.0.0.1:8182:8182 \
  -e gremlinserver.channelizer=org.apache.tinkerpop.gremlin.server.channel.WsAndHttpChannelizer \
  janusgraph/janusgraph@sha256:75f57aff4b152ca86b4cdaeefb5335154d7fad412cffef75fa44c7504adc96d4

docker run -d --name gsp-hugegraph \
  -p 127.0.0.1:8080:8080 \
  hugegraph/hugegraph@sha256:077b89e1f2d5e228d99598bdb537161fb2e8a7b5abedf4724d97cf4de11529ba
```

JanusGraph 默认模板为 BerkeleyJE／Lucene，环境变量配置双 HTTP／WebSocket channelizer。[官方容器配置](https://docs.janusgraph.org/v1.1/operations/container/) HugeGraph REST `/gremlin` 转发给原生 Gremlin Server；1.7.0 使用 `DEFAULT-hugegraph` 图绑定和 `__g_DEFAULT-hugegraph` 遍历源。[版本源码](https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/hugegraph-api/src/main/java/org/apache/hugegraph/auth/ContextGremlinServer.java)

分别检查 `configs/janusgraph.yaml`、`configs/hugegraph.yaml` 中的地址、绑定和预算后，将 `allow_reset` 改为 `true`。HugeGraph 默认使用表中 11 项 Pattern，已排除 P12。两份配置均要求 `language: gremlin`、`dedicated: true`；启用认证时填写 `user` 和 `password_env`，密码通过环境变量读取。

配置中的 `database: gsp_test` 是实验目标标识，**不能凭它隔离已有业务图**；真正重建范围由 `graph_binding`、`traversal_source` 指向的图决定。重建会删除该绑定图的数据；JanusGraph保留并检查实验schema，HugeGraph重建本工具的关系schema与成本索引，因此应使用上述独立实例。一次只运行一个测试目标。

```bash
python Src/main.py --config configs/janusgraph.yaml
python Src/main.py --config configs/hugegraph.yaml

python Src/main.py --config configs/janusgraph.yaml \
  --replay results/<运行>/<案例>
```

Gremlin 案例保存 `query_a.gremlin`、`query_b.gremlin`、可选 `query_ref.gremlin` 及 `result.json`。参数保存在 `query_bindings`，GraphSpec 保存在 `graph_spec`；`setup.cypher` 只是图配方的传统展示，不能直接发给 Gremlin Server。重放核验输入指纹、文本和参数，在新的结果目录中重建并执行，保留原始证据。已有 IR 案例跨目标重放会按目标重新生成查询；无 IR 的旧 Cypher 案例不能假装成为 Gremlin 输入。

真实语义对照通过显式环境变量启用，配置必须指向专用测试实例：

```bash
GRAPHSYMPERF_GREMLIN_TEST_CONFIG_DIR=/absolute/test/configs \
GRAPHSYMPERF_GREMLIN_NEO4J_CONFIG=/absolute/test/configs/neo4j.yaml \
GRAPHSYMPERF_GREMLIN_TEST_OUTPUT=/absolute/evidence/directory \
python -m pytest -m integration tests/test_gremlin_semantics.py
```

配置目录须包含 `janusgraph.yaml`；省略 Neo4j 环境变量时只检查构造期望结果，不能称为两库语义对照。测试不以发现新 BUG 为通过条件。

最终离线回归为 **1,171 passed，242 integration deselected**。另行完成上述 29 项真实语义对照、69 个小图输入、46 个扩规模输入、HugeGraph 8 次大图重建探测，以及同目标和跨目标各 1 个真实重放。全部 139 份案例存档（含明确排除的历史轮次）通过输入指纹、IR、文本、列名与类型敏感参数核验。[验证汇总](../results/gremlin-expansion-20261009/validation.json)和[环境信息](../results/gremlin-expansion-20261009/environment.json)保留于工作区；大型实验结果沿用仓库忽略规则，不随源码 commit 提交。

## ArcadeDB Gremlin 接入（2026-10-18）

新增目标为 **ArcadeDB 26.10.2，build `e854692736b48971f92dff78a1e727bc570bb6a2`，TinkerPop 3.8.2**。本轮复用仓库已有 ArcadeDB 数据装载能力，增加该 DBMS 的 Gremlin 语言适配器；不把它计为新引入的独立 DBMS，也不增加 Pattern、变换或原生探针。选择依据见 [新增目标选择](gremlin-target-selection-20261018.md)：已有维护响应和修复样本支持本轮选择，但不足以断言其总体影响力超过 HugeGraph。

实际版本、平台、镜像、堆设置与命令配置记录在 [环境证据](../results/gremlin-extension-20261018/environment.json)。本机使用的 ARM64 镜像 manifest 为 `arcadedata/arcadedb@sha256:7a498ebbbb9a5f02f7d43487c13b6632f83eb7fff345968baf220ac9266fd791`；这是该架构的固定摘要，不是可通用于所有架构的多架构索引。TinkerPop 版本依据 [26.10.2 的依赖声明](https://github.com/ArcadeData/arcadedb/blob/26.10.2/pom.xml)。

### 接口与语义边界

适配器通过 `/api/v1/query/{database}` 提交 `language: gremlin`、`command`、`params` 和结果上限，使用服务器默认的 Java Gremlin 引擎，不启用 Groovy 回退或额外 Gremlin Server 插件。SQL schema、JSONL 批量装载、类型及索引有效性检查复用原生 ArcadeDB 适配器；实验查询和图状态检查使用 Gremlin。P2 的逆关系与 P4／P5／P15 的参考关系检查使用不含匿名函数的等价遍历，保留原有检查含义。[官方 Gremlin 接口](https://docs.arcadedb.com/arcadedb/reference/gremlin/gremlin)

固定版本的 Java 解析器支持 `P.eq(_gsp_p0)` 等谓词参数，但不接受 `constant(_gsp_p0)`。因此仅对 ArcadeDB 目标的 `constant()` 参数，编译器生成经过 IR 类型校验的字面量：整数使用 `L` 后缀，浮点数使用 `D` 后缀，布尔与 NULL 保留类型，字符串使用单引号及严格转义；其余谓词值仍通过独立 `params` 传输。JanusGraph／HugeGraph 的既有参数生成方式保持不变。存档和重放同时核验 IR、实际文本、类型敏感参数及输入指纹，不能只比较查询文本。

原生 HTTP 返回平铺的投影 map；适配器保留 NULL、数值类型与重复行，检查 `returned`、`limit`、`truncated` 和 `max_rows + 1` 哨兵行，拒绝将截断结果认作完整结果。普通运行时间包含提交、完整响应读取与 JSON 解码，排除连接建立；未知连接中断或超时不自动重试，也不声称查询已经取消。

计划在普通计时之外独立采集：`profileExecution: detailed` 返回 `GremlinExecutionPlan`，保留原生 `cost`、`totalCost`、`elements`、`traversers` 和嵌套步骤。`cost`／`totalCost` 是 PROFILE 纳秒，分别表示当前步骤自身／含嵌套步骤的时间，不是普通查询运行时间；`.explain()` 经原生 command 端点返回遍历优化策略说明。HTTP 请求耗时元数据不冒充 Gremlin 执行计划，也不将其工作量命名为 Cypher 的 DB Hits。

### 已通过的接口门禁

首轮覆盖与 JanusGraph 接入一致：**P1、P2、P4、P5、P6、P8、P9、P10、P11、P12、P14、P15** 共 12 项。后续已为 ArcadeDB 增加 S1–S10，见下节；P3、P7、P13、P16 仍保持原有拒绝边界，没有以别的机制替代原 Pattern。

| 检查 | 数量 | 验证内容 |
|---|---:|---|
| 既有语义 fixture | 29 | 按构造期望结果检查 NULL／缺失属性、三值逻辑、标量类型、64 位边界、COUNT／EXISTS、重复行、自环／平行边和变量复用 |
| 常规 Pattern 输入 | 72 | 12 项 Pattern × 2 个种子 × 3 个复杂度；检查编译、装载、状态、A/B/reference 完整结果、原生计划、重新连接、独立重建和存档读回 |
| 既有状态变体 | 3 | P10 `empty_low`，P11 `empty_low`、`multi_low` |
| 损坏图拒绝 | 3 | 错误逆边配对键、缺失逆边、缺失参考边均被原有状态检查拒绝；重建后恢复通过 |
| 离线回归 | 1,250 | 协议、编译、计时边界、结果完整性、归档及既有后端回归；349 项集成测试在该次离线命令中未运行 |

共 **107 项真实接口检查通过**，接口门禁完成后才启动性能实验。29 项语义 fixture 复用既有测试内容，本轮与构造期望结果比较；不能将历史 JanusGraph／Neo4j 对照说成本轮重新运行的跨库对照。证据见 [门禁记录](../results/gremlin-extension-20261018/interface-gate.json)、[104 项正常接口检查](../results/gremlin-extension-20261018/interface/validation-summary.json)、[3 项损坏图检查](../results/gremlin-extension-20261018/interface/negative-state-checks/validation-summary.json) 和 [离线输出](../results/gremlin-extension-20261018/offline-tests.txt)。这些检查通过不等于发现遗漏优化，也不证明该版本不存在性能缺陷。

本轮 `arcadedb.command.timeout=30000` 是 SQL 等命令的配置，不能据此宣称 Gremlin 服务端具有相同超时：固定版本将 Gremlin 归入独立的 `polyglotCommand.timeout` 设置。这里验证的是客户端 `timeout_seconds=30` 后停止目标，未验证 Gremlin 原生取消；发生超时后须确认服务端终止或重启专用实例再继续。[固定版本超时定义](https://github.com/ArcadeData/arcadedb/blob/26.10.2/engine/src/main/java/com/arcadedb/GlobalConfiguration.java#L766-L825)

### 专用实例运行方式

以下启动示例针对本机已验证的 ARM64 镜像，在环境变量中自行设置专用测试密码；不使用本地实验凭据。其他架构应使用对应架构的固定发行镜像。

```bash
export GRAPHSYMPERF_ARCADE_GREMLIN_PASSWORD='<专用测试实例密码>'

docker run -d --name gsp-arcade-gremlin \
  -p 127.0.0.1:2488:2480 \
  -e JAVA_OPTS="-Xms1g -Xmx4g -Darcadedb.server.rootPassword=${GRAPHSYMPERF_ARCADE_GREMLIN_PASSWORD} -Darcadedb.server.defaultDatabases=gsp_gremlin[root] -Darcadedb.command.timeout=30000" \
  arcadedata/arcadedb@sha256:7a498ebbbb9a5f02f7d43487c13b6632f83eb7fff345968baf220ac9266fd791
```

检查 [`configs/arcadedb-gremlin.yaml`](../configs/arcadedb-gremlin.yaml) 的地址、数据库、密码环境变量及预算，确认 `gsp_gremlin` 为可重建的专用数据库后，将 `allow_reset` 改为 `true`；保留 `dedicated: true`。ArcadeDB 的目标由 HTTP 路径中的实际数据库名确定，构图会重建该库的实验类型、记录和索引。先执行接口检查，再单独启动性能测试；同一实例不并行运行两轮实验。

```bash
GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG="$PWD/configs/arcadedb-gremlin.yaml" \
GRAPHSYMPERF_ARCADE_GREMLIN_TEST_OUTPUT="$PWD/results/arcadedb-gremlin-interface" \
python -m pytest -m integration \
  tests/test_arcade_gremlin_integration.py tests/test_arcade_gremlin_negative.py \
  tests/test_arcade_gremlin_scor_integration.py

python Src/main.py --config configs/arcadedb-gremlin.yaml

python Src/main.py --config configs/arcadedb-gremlin.yaml \
  --replay results/<运行>/<案例>
```

配置沿用现有测量流程：初筛阈值 `screen_ratio: 2`，两次独立重建确认，每次 5 次预热、20 个平衡 AB/BA 对，并执行 A/A、B/B 噪声对照。issue 筛选配置为 **中位比值 ≥ 5、置信区间下界 ≥ 5、中位差值 > 100 ms、方向一致性 ≥ 0.95**。显式遍历的稳定差异仍只自动记为 `timing_observation`；进入 `BUG list` 前还需核查独立确认结果、适用优化规则、可避免工作和根因去重。正式性能结果与报告筛选另由 [2026-10-18 Gremlin 实验记录](gremlin-experiment-20261018.md) 记录。


### 同日后续：S1–S10 与 P10 扩规模

ArcadeDB 的 Gremlin 能力名单现为上述 12 项 P 加 **S1–S10，共 22 项现有模板**。S1、S2、S4–S10 复用已具备的表达式和查询生成；S3 增加顶层条件查询的 `choose()` 编译，并将 Cypher 25 探测限制到 Cypher 路径。没有修改生成器、变换器、IR 或 GraphSpec 配方；JanusGraph／HugeGraph 的 S 覆盖未在本轮实测，名单保持原样。

性能测试前新增真实门禁为 **290 项通过**：270 个既有 S 输入（10 项 × 9 个代表种子 × 3 档复杂度）、12 个条件查询、5 个具有非空预期的 NULL-safe 成员查询、3 个假值非 NULL 的 COALESCE 查询。收尾再补充并通过 1 项运行表达式产生 NULL 的条件守卫检查，合计 **291 项**。13 个条件查询均核对真实计划中的未选分支行数／遍历对象数为零；不以 PROFILE 初始化时间替代执行工作量。最终离线回归为 **1,362 passed，640 deselected**。[门禁输出](../results/gremlin-p10-scor-20261018/scor-interface-tests.txt)、[新增动态 NULL 回归](../results/gremlin-p10-scor-20261018/dynamic-null-interface-test.txt)、[语义与状态记录](../results/gremlin-p10-scor-20261018/interface/scor/validation-summary.json)

现有查询对、原阈值和独立确认流程均保留。扩规模 P10 的六种实际变体、更多数据标识种子及 S1–S10 性能结果见 [续测记录](gremlin-p10-scor-20261018.md)。四项在 ArcadeDB 名单中仍未开放的 P 模板不能笼统归因为数据库缺功能，详见 [P3／P7／P13／P16 编译边界](gremlin-pattern-boundaries.md)。


## 2026-10-18：JanusGraph／HugeGraph 补齐 P3、P13、P16

该 P3／P13／P16 轮次完成时，JanusGraph 支持 **15 项 P（P1–P16 除 P7）**，HugeGraph 支持 **14 项 P（除 P7、P12）**。三个新开放 Pattern 的编译和真实接口均已验证；该轮没有声称 S1–S10 在这两个目标也完成相同门禁。ArcadeDB 保持 12 项 P 加 S1–S10，新增三项没有冒充其已验证能力。随后 S1–S10 的实际接入见下一节。

P3 使用 repeat 和每条路径的边身份历史实现 TRAIL，允许顶点重访，保留路径长度及节点谓词。HugeGraph P3 用原生父／子边标签保存同一逻辑关系跨多种端点的图配方，实际查询保留原逻辑类型；装载与状态检查记录物理映射及身份。这不覆盖原始边标签投影，不顺带开放 P12 或改变 P7。

P13 明确传出可选分支的新绑定并在无匹配时 NULL 延展；HugeGraph 旧版对实体使用内部非 NULL 哨兵，最终输出仍为真实 NULL，标量 FALSE 保持 FALSE。P16 的列表在服务器构造，整数范围通过 Long 与 sack 累加产生，以数值 until 终止避免旧版 short 循环计数回绕。范围端点使用严格整数 Long 字面量，其余原有谓词参数继续独立绑定；没有客户端展开列表或折叠实验谓词。

JanusGraph 当前整套接口重跑 **101 passed、1 项 HugeGraph 专用跳过**；HugeGraph 在全新存储、同一服务器进程中重新完成完整套件 **102 passed**，另通过大图装载／清理门禁。每库含 1 项编译拒绝检查，其余涵盖完整类型多重集、NULL、重复行、自环／平行边、精确范围、原有图态、计划与归档。当前离线回归为 **1,422 passed，742 deselected**。初次门禁与修复前记录均保留，见完整扩展记录。

**HugeGraph P3 的已验证范围限定为同一服务器进程。** 1.7.0 RocksDB 的父／子边 schema 序列化未保留类型及 fatherId；本轮实测重启后边 ID 改变且旧边无法按恢复出的 ID 删除。执行器现在在清理前检测此状态并停止，不再无限重试。P3 性能使用全新空存储，并在整个测量期间保留同一 JVM；不会把异常存储上的结果计入性能。见 [重启诊断](../results/gremlin-extended-20261018/hugegraph-parent-restart-diagnosis.md)。

使用专用配置、允许重建后，依次对两个实例执行扩展接口检查：

```bash
GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_CONFIG="$PWD/configs/janusgraph.yaml" \
GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_OUTPUT="$PWD/results/janusgraph-extended-interface" \
python -m pytest -x -m integration tests/test_gremlin_extended_integration.py

GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_CONFIG="$PWD/configs/hugegraph.yaml" \
GRAPHSYMPERF_GREMLIN_EXTENDED_TEST_OUTPUT="$PWD/results/hugegraph-extended-interface" \
python -m pytest -x -m integration tests/test_gremlin_extended_integration.py
```

仅在对应接口门禁通过后使用既有 main.py 流程运行性能测试。实现、原阈值、种子去重、结果与报告判定见 [完整扩展记录](gremlin-extended-20261018.md)。全部继续区分显式遍历耗时观察与源码／计划支持的优化遗漏。

本轮两库各完成 56 个既定输入，另完成缩减及原存档重放。最终本地报告为 JanusGraph P3、HugeGraph P3 和 HugeGraph P13；三份报告的建图与查询又在新容器通过全图和完整结果核验。两份 P3 共享前缀剪枝机会，P13 为既有 OPTIONAL 重复计算机制的新增 DBMS 复现，均未提交上游。[报告及精确数值](gremlin-extended-20261018.md#已登记的三份报告)。

## 2026-10-18 后续：JanusGraph／HugeGraph 接入 S1–S10

当前执行入口为 JanusGraph **25 项（15P+10S）**、HugeGraph **24 项（14P+10S）**；P7 及 HugeGraph P12 的原有边界保持。HugeGraph 的名单表示每个 S 模板有原生可执行的受限形式，不保证全部种子组合可用。

**当前执行范围已按用户要求收窄为 JanusGraph。** HugeGraph 测试及专用容器已停止，后续普通测量、N=100,000 扩规模、缩减重放和本轮新报告均取消；下列 HugeGraph 能力与门禁保留作既有实测历史，不再表示待完成目标。JanusGraph 的 v5 冻结源码、429 项通过门禁及原方法保持有效。

接口种子与性能候选池统一为 `0,1,2,3,6,9,12,18,27,36,54,81`。在诊断结果上限对齐后的 v5 冻结源码与新空库上，JanusGraph **429 passed**；HugeGraph **301 passed、128 unsupported_feature**；离线回归为 **1,528 passed、1,171 integration deselected**。每库计划 360 项生成模板检查（10S×12种子×3复杂度），另有 68 项独立结果语义和 1 项物理属性映射检查。HugeGraph 的 128 项仅为原始 schema 下 `ScorRoot.uid` 未索引导致的精确原生拒绝，其余语义、装载、NULL、重复行、分支、完整结果和接口检查照常要求通过；没有将其他错误或超时跳过。[v5 冻结门禁](../results/gremlin-scor-janus-huge-20261018-v5/interface-gate.json)。

v4 因普通 execute 与 EXPLAIN／PROFILE 的结果上限包装不一致而在扩规模前停止，原始记录保留。v5 只使诊断遍历追加与普通执行相同的上限，普通查询、Pattern、图配方及测量协议不变；新小图门禁已确认 S5 的两个 Range 合并为 NoneStep，分别消费 12 条 HIGH／3 条 REF 后返回空结果。[有界计划校准](../results/gremlin-scor-janus-huge-20261018-v5/source-audit/S5-bounded-plan-calibration.md)属于语义与工作量证据，不是普通运行时过阈值结论。

JanusGraph 保护属性名采用可逆物理映射，逻辑 `key` 保留在 IR／GraphSpec 中，实际存储为 `_gsp_property_key`，同前缀逻辑名称另行转义以防碰撞。HugeGraph 的用户边属性允许缺失并返回 NULL，sort key `_gsp_edge_ordinal` 继续必填。这两项为接口适配，未改 Pattern、拓扑、索引设计或测量方法。

HugeGraph 1.7.0 在缺少属性索引时不对原 `.has('uid',...)` 查询自动回退全扫。本轮保留原查询和原 `ScorRoot.id` 索引，逐输入记录支持状态；没有额外建 uid 索引、关闭优化策略或加入不透明过滤。未选分支可能使其他包含 uid 的输入正常短路，因此不禁用整个种子或 Pattern。[固定版本边界](../results/gremlin-scor-janus-huge-20261018/source-audit/hugegraph-unindexed-uid.md)。

本轮按用户要求只续测 JanusGraph，最终覆盖为 **164 个唯一原计划输入＝150 主输入＋14 条件输入**，状态为 **133 no_candidate／26 not_confirmed／5 timing_observation**。四个 S5 条件槽位 seed=1／3／9／12 明确未执行，未计性能阴性，遗漏槽位为零。175 个物理存档还包括10次有限缩减／验证执行及一次旧外部休眠中断，不能冒充175个原输入。五组原始统计审计的155／8／10／1／1档全部零错误，三个选中代表各自40对普通确认样本通过原合并、逐次和噪声门槛；见[最终统计审计](../results/gremlin-scor-janus-huge-20261018-v5/recovery/final-statistical-audit-review.json)和[覆盖核对](../results/gremlin-scor-janus-huge-20261018-v5/recovery/coverage.json)。

经过有限缩减、独立重放、精确程序与全图新空库验证，已登记两份本地报告：[S5／BUG004](../BUG%20list/JanusGraph/S5/BUG004.md)，N=5,000、seed=3，配对倍率70.214619×、差值109.139710ms；[S6／BUG005](../BUG%20list/JanusGraph/S6/BUG005.md)，N=100,000、seed=0，184.037156×、254.459064ms。S8 的最终 N=100,000 重放161.723316×、248.632062ms通过门槛，但相同 EntryList 邻接预装载路径按机制并入S6，不另开报告。S5 的新空库全图为20,004顶点／20,003边，S6为400,004／400,003；S8在物理流字段与全量逐行一致证明后复用同一S6新图进行只读实际验证，没有独立新增容器或普通性能样本。

S5 尾部 limit(0) 的 N=100,000、seed=0 原槽位正常完成，倍率400.144603×，但差值2,181.268354ms低于噪声p99的2,364.013987ms，未确认、不报告、不额外重放。所有未确认缩减与失败尝试原样保留；旧休眠中断的S6 seed=6只独立恢复重放一次，前后样本不混合，原四小时条件预算未扩。HugeGraph 剩余测试和本轮新报告继续按用户要求取消，已有80个完整输入及[旧进度](../results/gremlin-scor-janus-huge-20261018-v5/before-janus-only-matrix-progress.json)保留。

性能控制器已正常结束并停止其专用JanusGraph，两个本轮新复现容器也已停止，八个冻结源码散列未变。最终独立报告审稿与原生证据复核已通过，根状态已标记 `completed`、`task_complete=true`、`report_ready=true`；[最终资格记录](../results/gremlin-scor-janus-huge-20261018-v5/final-qualification.json)保存逐项证据。两份报告为本地 `reproduced`／`local_experiment`，**未上游提交、未获开发者确认**，独立跨库根因数未知。[本地登记记录](../results/gremlin-scor-janus-huge-20261018-v5/report-validation/local-report-publication.json)和[本轮完整记录](gremlin-scor-janus-huge-20261018.md)保留证据与边界。

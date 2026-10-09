# Gremlin 实测覆盖

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

## 当前支持范围

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

常量不拼接进遍历文本。每个 A、B、reference 保存自己的 `query_bindings`；文本相同但参数不同仍是不同输入。NULL 比较保留三值逻辑，`NOT` 测试 FALSE 而不是简单把“未匹配 TRUE”当作 FALSE；缺失属性投影为 NULL。`gremlin_null_projection` 必须经过版本验证后显式启用，不能用配置覆盖已知语义不兼容。

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

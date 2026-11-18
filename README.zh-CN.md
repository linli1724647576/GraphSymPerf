# GraphSymPerf

[English](README.md) · [文档](docs/README.md) · [缺陷报告](docs/bug-reports.md)

GraphSymPerf 使用短路变换和对称变换测试图数据库的性能。程序先生成种子查询的抽象语法树（abstract syntax tree, AST），嵌入一个适用的测试模式（pattern），再为查询对构造图数据。随后比较完整查询结果、测量执行时间，并采集执行计划以分析性能差异。

Cypher 接入 Neo4j、Memgraph、ArcadeDB、LadybugDB 和 FalkorDB；Gremlin 接入 ArcadeDB、JanusGraph 和 HugeGraph。[模式目录](docs/pattern-catalog.zh-CN.md)说明八个短路模式（SC1–SC8）和八个对称模式（SY1–SY8）。

<!-- BEGIN ISSUE SUMMARY -->
下表列出 48 份已提交报告：25 份已修复，10 份已确认，13 份待确认。状态核查日期：2026-11-18。完整台账、状态依据和复现报告见[报告索引](docs/bug-reports.md)。
<!-- END ISSUE SUMMARY -->

<!-- BEGIN ISSUE TABLES -->
## Neo4j

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [13783](https://github.com/neo4j/neo4j/issues/13783) | 已修复 |
| 2 | [13866](https://github.com/neo4j/neo4j/issues/13866) | 已确认 |
| 3 | [14008](https://github.com/neo4j/neo4j/issues/14008) | 已修复 |

## Memgraph

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [4208](https://github.com/memgraph/memgraph/issues/4208) | 已确认 |
| 2 | [4456](https://github.com/memgraph/memgraph/issues/4456) | 待确认 |
| 3 | [4457](https://github.com/memgraph/memgraph/issues/4457) | 待确认 |
| 4 | [4458](https://github.com/memgraph/memgraph/issues/4458) | 待确认 |
| 5 | [4459](https://github.com/memgraph/memgraph/issues/4459) | 已修复 |
| 6 | [5091](https://github.com/memgraph/memgraph/issues/5091) | 待确认 |

## ArcadeDB

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [4405](https://github.com/ArcadeData/arcadedb/issues/4405) | 已修复 |
| 2 | [5116](https://github.com/ArcadeData/arcadedb/issues/5116) | 已修复 |
| 3 | [5117](https://github.com/ArcadeData/arcadedb/issues/5117) | 已修复 |
| 4 | [5360](https://github.com/ArcadeData/arcadedb/issues/5360) | 已修复 |
| 5 | [5362](https://github.com/ArcadeData/arcadedb/issues/5362) | 已修复 |
| 6 | [5363](https://github.com/ArcadeData/arcadedb/issues/5363) | 已修复 |
| 7 | [9579](https://github.com/ArcadeData/arcadedb/issues/9579) | 已修复 |
| 8 | [9580](https://github.com/ArcadeData/arcadedb/issues/9580) | 已修复 |
| 9 | [10307](https://github.com/ArcadeData/arcadedb/issues/10307) | 已修复 |
| 10 | [10308](https://github.com/ArcadeData/arcadedb/issues/10308) | 已修复 |
| 11 | [11081](https://github.com/ArcadeData/arcadedb/issues/11081) | 已修复 |
| 12 | [11082](https://github.com/ArcadeData/arcadedb/issues/11082) | 已修复 |
| 13 | [11083](https://github.com/ArcadeData/arcadedb/issues/11083) | 已修复 |

## LadybugDB

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [668](https://github.com/LadybugDB/ladybug/issues/668) | 已修复 |
| 2 | [669](https://github.com/LadybugDB/ladybug/issues/669) | 已修复 |
| 3 | [696](https://github.com/LadybugDB/ladybug/issues/696) | 已修复 |
| 4 | [697](https://github.com/LadybugDB/ladybug/issues/697) | 已修复 |
| 5 | [698](https://github.com/LadybugDB/ladybug/issues/698) | 已修复 |
| 6 | [720](https://github.com/LadybugDB/ladybug/issues/720) | 已修复 |
| 7 | [721](https://github.com/LadybugDB/ladybug/issues/721) | 已修复 |
| 8 | [1483](https://github.com/LadybugDB/ladybug/issues/1483) | 已修复 |

## FalkorDB

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | 已修复 |
| 2 | [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) | 已确认 |
| 3 | [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | 已确认 |
| 4 | [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) | 已确认 |
| 5 | [3637](https://github.com/FalkorDB/FalkorDB/issues/3637) | 已确认 |

## Apache AGE

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [2480](https://github.com/apache/age/issues/2480) | 已确认 |
| 2 | [2481](https://github.com/apache/age/issues/2481) | 待确认 |

## JanusGraph

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [5113](https://github.com/JanusGraph/janusgraph/issues/5113) | 待确认 |
| 2 | [5148](https://github.com/JanusGraph/janusgraph/issues/5148) | 待确认 |
| 3 | [5147](https://github.com/JanusGraph/janusgraph/issues/5147) | 待确认 |

## TinkerPop

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [TINKERPOP-3316](https://issues.apache.org/jira/browse/TINKERPOP-3316) | 待确认 |
| 2 | [TINKERPOP-3315](https://issues.apache.org/jira/browse/TINKERPOP-3315) | 待确认 |
| 3 | [TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311) | 已确认 |
| 4 | [TINKERPOP-3312](https://issues.apache.org/jira/browse/TINKERPOP-3312) | 待确认 |
| 5 | [TINKERPOP-3313](https://issues.apache.org/jira/browse/TINKERPOP-3313) | 已确认 |
| 6 | [TINKERPOP-3314](https://issues.apache.org/jira/browse/TINKERPOP-3314) | 已确认 |

## HugeGraph

| 序号 | 问题编号 | 状态 |
| --- | --- | --- |
| 1 | [3479](https://github.com/apache/hugegraph/issues/3479) | 待确认 |
| 2 | [3480](https://github.com/apache/hugegraph/issues/3480) | 待确认 |
<!-- END ISSUE TABLES -->

## 运行方法

### 1. 环境要求

使用 Python 3.12，并选择下表中的数据库。每个执行用例会替换指定实验图的数据，请使用专用数据库。

| 数据库 | 部署示例使用的版本 | 查询语言 |
| --- | --- | --- |
| Neo4j | Community 2026.09.0 | Cypher 25 |
| Memgraph | 3.14.0 | Cypher |
| ArcadeDB | 26.10.2 | Cypher、Gremlin |
| LadybugDB | 0.22.1 | Cypher |
| FalkorDB | 6.0.3 | Cypher |
| JanusGraph | 1.2.0 / TinkerPop 3.8.2 | Gremlin |
| HugeGraph | 1.7.0 / TinkerPop 3.5.1 | Gremlin |

服务启动命令和连接参数见[数据库部署](docs/backends.md)。LadybugDB 在 Python 进程内运行。

### 连接参数的配置位置

程序从当前工作目录的 [config.yaml](config.yaml) 读取全部运行配置。

| 配置段 | 用途 |
| --- | --- |
| `language`、`database` | 查询语言、数据库接口与连接参数 |
| `run` | 运行模式、种子、测试模式、时长与输出目录 |
| `generation`、`graph` | 查询生成与图规模 |
| `measurement` | 预热、配对测量、筛选阈值与查询超时 |
| `replay` | 归档路径与重放方式 |

`password_env` 等密码字段填写环境变量名。`config/janusgraph/` 和 `config/hugegraph/` 存放对应服务的配置文件。

### 2. 安装依赖

在仓库目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

[requirements.txt](requirements.txt)固定了数据库驱动和配置解析库的版本。

### 3. 运行参数

修改 `config.yaml` 中的 `run` 和 `measurement` 配置段：

```yaml
run:
  mode: experiment
  seed: 0
  case_seed: null
  save_all: false
  rounds: 10
  minutes: 60
  output: results
  patterns: [SC1, SC2, SC3, SC4, SC5, SC6, SC7, SC8, SY1, SY2, SY3, SY4, SY5, SY6, SY7, SY8]
measurement:
  warmups: 3
  pairs: 6
  confirmation_warmups: 5
  confirmation_pairs: 20
  ratio: 5.0
  minimum_delta_ms: 100.0
  direction_consistency: 0.8
  scale_factor: 10
  growth_ratio: 3.0
  timeout_seconds: 10
```

| 运行模式 | 行为 |
| --- | --- |
| `generate` | 生成并归档用例，无需连接数据库 |
| `check` | 装载图数据并比较完整查询结果 |
| `experiment` | 检查结果、测量执行时间并采集执行计划 |

`seed` 控制运行调度。设置 `case_seed` 后，程序使用同一个固定种子，对每个选定模式各尝试一次。普通运行每轮尝试所有选定模式。程序在启动新用例前检查时间预算，正在执行的用例可能晚于预算结束。

计时结果同时满足耗时比、中位配对耗时差和慢侧方向一致率三个阈值后，程序使用新样本复测确认。启用 `scale_factor` 时，程序还会在较小的图上重测 `no_gap` 查询对，检查耗时差是否随规模增大。设置 `scale_factor: null` 可关闭这一步。测量指标和结果状态见[架构文档](docs/architecture.zh-CN.md)。

### 4. 数据库连接

默认配置使用 Neo4j：

```yaml
language: cypher
database:
  system: neo4j
  uri: bolt://127.0.0.1:7687
  user: neo4j
  password_env: GRAPHSYMPERF_NEO4J_PASSWORD
  name: neo4j
  batch_size: 5000
```

```bash
export GRAPHSYMPERF_NEO4J_PASSWORD='your-local-experiment-password'
```

其他数据库使用[数据库部署](docs/backends.md)中的对应连接块。Gremlin 运行需设置 `language: gremlin`。Cypher SC7 仅在 Neo4j 启用，其他 Cypher 数据库需从 `run.patterns` 移除 SC7；Gremlin 支持全部十六个模式。

### 5. 查询生成参数

```yaml
generation:
  clauses: 4
  nodes: 100
  expression_depth: 3
  subquery_depth: 3
  conditional_queries: true
graph:
  background_nodes: 50
  background_relationships: 100
  high_units: 100000
  indexes: true
```

`generation.nodes` 设置语法树展开预算。`graph.high_units` 设置当前模式的图扩张预算，按锚点或区域分配。最终图还包括随机背景图和模式要求的固定结构。随机查询中没有适用的嵌入位置时，用例记为 `skipped`。

### 6. 执行命令

```bash
python main.py
```

重放归档时，将 `replay.case` 设为用例目录。`replay.check_only: true` 只检查结果，`false` 则继续测量性能：

```bash
python replay.py
```

固定种子和图规模的批量实验按[批量配置说明](docs/architecture.zh-CN.md#10-固定种子运行)设置后执行：

```bash
python scripts/run_campaign.py
```

更新 [bugs.csv](bugs.csv) 后，重新生成两个 README 的缺陷摘要和表格：

```bash
python scripts/update_readme.py
```

### 7. 输出文件

| 输出 | 内容 |
| --- | --- |
| `results/<run>/summary.csv` | 用例编号、模式、种子、结果状态、图规模和计时摘要 |
| `query_a.cypher`、`query_b.cypher` | 查询对；Gremlin 用例使用 `.gremlin` 扩展名 |
| `graph.json` | 图数据、模式定义与请求的索引 |
| `result.json` | 配置、数据库版本、查询参数、结果状态与测量样本 |
| `plans.json` | 执行计划和算子摘要 |
| `plans-small.json` | 规模确认阶段采集的较小图执行计划 |
| `replay-result.json` | 最近一次重放的结果 |
| `attempts.json`、`report.json` | 固定批量实验的计划清单和完成摘要 |

生成的用例、性能候选、结果差异、超时、数据库拒绝和执行失败都会归档。设置 `run.save_all: true` 可同时保留普通完成用例。查询终止无法确认时，程序停止运行。

`results/` 存放本地输出，不纳入 Git。测试与验收步骤见[测试说明](docs/testing.md)，正式复现报告见 [bug-reports/](bug-reports/README.md)。

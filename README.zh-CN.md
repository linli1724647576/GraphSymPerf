# GraphSymPerf

GraphSymPerf 测试图数据库中的短路与对称变换。程序先生成符号查询 AST，选择一个兼容位置嵌入 Pattern，再构造包含固定见证和扩张区域的图，比较同图上的查询对。Cypher 接入 Neo4j、Memgraph、ArcadeDB、LadybugDB 和 FalkorDB；Gremlin 接入 ArcadeDB、JanusGraph 和 HugeGraph。

<!-- BEGIN ISSUE SUMMARY -->
This README lists 33 submitted issue URLs: 21 fixed, 6 confirmed, 6 pending. Status snapshot: 2026-10-29. Historical and merged reports are included; counts do not represent unique root causes.
<!-- END ISSUE SUMMARY -->

下表从唯一维护的 [bugs.csv](bugs.csv) 生成，包含历史报告和合并记录，不代表当前重写版本发现的独立根因数。实现细节见[中文架构文档](docs/architecture.zh-CN.md)、[后端部署](docs/backends.md)和[验证记录](docs/validation.md)。[English](README.md)。章节顺序和表格格式参照 [ValScope](https://github.com/linli1724647576/ValScope/blob/e158a9d467a343f985427d04624fb44ac7895fa9/README.md)。

<!-- BEGIN ISSUE TABLES -->
## Neo4j

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [13783](https://github.com/neo4j/neo4j/issues/13783) | fixed |
| 2 | [13866](https://github.com/neo4j/neo4j/issues/13866) | confirmed |
| 3 | [14008](https://github.com/neo4j/neo4j/issues/14008) | fixed |

## Memgraph

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [4208](https://github.com/memgraph/memgraph/issues/4208) | confirmed |
| 2 | [4456](https://github.com/memgraph/memgraph/issues/4456) | pending |
| 3 | [4457](https://github.com/memgraph/memgraph/issues/4457) | pending |
| 4 | [4458](https://github.com/memgraph/memgraph/issues/4458) | pending |
| 5 | [4459](https://github.com/memgraph/memgraph/issues/4459) | fixed |
| 6 | [5091](https://github.com/memgraph/memgraph/issues/5091) | pending |

## ArcadeDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [4405](https://github.com/ArcadeData/arcadedb/issues/4405) | fixed |
| 2 | [5116](https://github.com/ArcadeData/arcadedb/issues/5116) | fixed |
| 3 | [5117](https://github.com/ArcadeData/arcadedb/issues/5117) | fixed |
| 4 | [5360](https://github.com/ArcadeData/arcadedb/issues/5360) | fixed |
| 5 | [5362](https://github.com/ArcadeData/arcadedb/issues/5362) | fixed |
| 6 | [5363](https://github.com/ArcadeData/arcadedb/issues/5363) | fixed |
| 7 | [9579](https://github.com/ArcadeData/arcadedb/issues/9579) | fixed |
| 8 | [9580](https://github.com/ArcadeData/arcadedb/issues/9580) | fixed |
| 9 | [10307](https://github.com/ArcadeData/arcadedb/issues/10307) | fixed |
| 10 | [10308](https://github.com/ArcadeData/arcadedb/issues/10308) | fixed |

## LadybugDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [668](https://github.com/LadybugDB/ladybug/issues/668) | fixed |
| 2 | [669](https://github.com/LadybugDB/ladybug/issues/669) | fixed |
| 3 | [696](https://github.com/LadybugDB/ladybug/issues/696) | fixed |
| 4 | [697](https://github.com/LadybugDB/ladybug/issues/697) | fixed |
| 5 | [698](https://github.com/LadybugDB/ladybug/issues/698) | fixed |
| 6 | [720](https://github.com/LadybugDB/ladybug/issues/720) | fixed |
| 7 | [721](https://github.com/LadybugDB/ladybug/issues/721) | fixed |

## FalkorDB

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) | fixed |
| 2 | [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) | confirmed |
| 3 | [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) | confirmed |
| 4 | [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) | confirmed |

## Apache AGE

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [2480](https://github.com/apache/age/issues/2480) | confirmed |
| 2 | [2481](https://github.com/apache/age/issues/2481) | pending |

## JanusGraph

| ID   | Issue ID | Status |
| ---- | -------- | ------ |
| 1 | [5113](https://github.com/JanusGraph/janusgraph/issues/5113) | pending |
<!-- END ISSUE TABLES -->

## How to Run

### 1. Environment Requirements
- Python 3.12；Ladybug CSV 装载使用 `csv.QUOTE_NOTNULL`。
- 选择一个已接入的数据库：
  - Neo4j Community 2026.09.0／Cypher 25，默认配置端口 7687。
  - Memgraph 3.14.0，部署示例端口 27687。
  - ArcadeDB 26.10.2，部署示例端口 22480。
  - LadybugDB 0.22.1，进程内 pybind 后端。
  - FalkorDB 6.0.3，部署示例端口 16379。
  - JanusGraph 1.2.0／TinkerPop 3.8.2，BerkeleyJE，部署示例端口 38182。
  - HugeGraph 1.7.0／TinkerPop 3.5.1，RocksDB，部署示例端口 48080、48182。

### DBMS Connection Parameter Configuration Locations

连接参数统一在 [config.yaml](config.yaml) 的 `database` 段设置，密码通过指定环境变量读取。

- **主配置**：`config.yaml` 中的 `language`、`database`、`generation`、`graph`、`run` 和 `measurement`。
- **数据库接口**：`databases/`；能力字典决定可用语法及 Pattern 变体。
- **服务端配置**：`config/janusgraph/`、`config/hugegraph/`。完整启动命令和连接块见[后端部署](docs/backends.md)。

### 2. Install Dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

主要依赖为 `neo4j==5.28.4`、`PyYAML==6.0.3`、`ladybug==0.22.1`、`falkordb==1.8.1` 和 `redis==8.1.0`。三个 HTTP 后端使用 Python 标准库。

### 3. Running Parameter Configuration

保留其他配置段，按任务修改运行参数：

```yaml
language: cypher
run:
  mode: experiment
  seed: 0
  case_seed: null
  save_all: false
  rounds: 10
  minutes: 60
  output: results
  patterns: [S1, S2, S3, S4, S5, S6, S7, S8, Y1, Y2, Y3, Y4, Y5, Y6, Y7, Y8]
measurement:
  warmups: 3
  pairs: 6
  confirmation_warmups: 5
  confirmation_pairs: 20
  ratio: 5.0
  minimum_delta_ms: 100.0
  direction_consistency: 0.8
  timeout_seconds: 10
```

`generate` 只生成归档；`check` 装载图并比较完整结果；`experiment` 继续计时和采集 PROFILE。固定 `case_seed` 时只跑一轮。Cypher S7 仅在 Neo4j 启用；Gremlin 使用 S1–S6、S8、Y1、Y2。

### 4. Database Configuration

默认连接专用 Neo4j 实验库：

```yaml
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

每个执行用例会替换指定实验图的数据。Q1/Q2 之间图与索引保持不变。其他系统的连接配置见[后端部署](docs/backends.md)。

### 5. Query Generation Configuration

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

`generation.nodes` 是语法展开预算。`high_units` 是用例内的高成本扩张单元总数，按锚点或区域分配，不等于节点总数。随机上下文允许不适用或无法触达目标，程序不使用求解器修补这些上下文。

### 6. Execution Command

```bash
python main.py
```

在 `replay.case` 指定归档路径，并设置 `replay.check_only` 后执行：

```bash
python replay.py
```

固定种子／规模运行需增加[架构文档](docs/architecture.zh-CN.md#10-固定种子运行)中的 `campaign` 配置，设置 `save_all: true` 和新输出目录，然后执行：

```bash
python scripts/run_campaign.py
```

维护 `bugs.csv` 后，重新生成两个 README 的 issue 表格：

```bash
python scripts/update_readme.py
```

### 7. Output Description
- **`results/<run>/summary.csv`**：用例、Pattern、种子、状态、图规模和可用计时信息。
- **用例归档**：原查询对、`graph.json`、`result.json`、`plans.json`；请求重放后另存 `replay-result.json`。
- **固定清单运行**：`attempts.json` 保留所有计划项及未提交项；`report.json` 记录停止原因、计数、耗时和客户端资源。
- **本地证据**：`results/` 被 Git 忽略，已记录实验的范围见[验证记录](docs/validation.md)。

性能候选、结果差异、查询超时、已识别拒绝、执行异常及终止未确认的用例都会归档。`run.save_all: true` 同时保留普通用例。终止未确认时停止后续请求，不重启数据库。

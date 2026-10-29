# GraphSymPerf 中文说明

[English README](README.md)

GraphSymPerf 从随机符号 AST 出发，选择一个兼容位置嵌入 Pattern，构造包含小规模和大规模区域的同一张图，再比较原查询与一次局部变异后的查询。

**程序会清空配置中指定的数据库，并为每个用例重新装载图。请使用专用实验库。** 同一个查询对执行期间，图和索引保持不变。

## 安装与运行

需要 Python 3.12，以及支持 Cypher 25 的 Neo4j。当前验收使用 Neo4j Community 2026.09.0。

安装依赖：

    uv venv --python 3.12
    uv pip install -r requirements.txt

在 `config.yaml` 中设置连接信息，并通过环境变量提供密码：

    export GRAPHSYMPERF_NEO4J_PASSWORD='your-password'
    .venv/bin/python main.py

所有实验参数都在 `config.yaml` 中设置。在仓库目录下运行：

    .venv/bin/python main.py

通过 `run.mode` 选择运行方式：

| 模式 | 行为 |
| --- | --- |
| `experiment` | 装载图，比较结果并检查性能 |
| `generate` | 生成并保存用例，不连接数据库 |
| `check` | 装载图并比较完整结果，不进行计时 |

例如，生成 Y4、种子 24、100 个扩张单元的用例，修改现有配置中的这些字段，其他字段保留：

    run:
      mode: generate
      patterns: [Y4]
      case_seed: 24
    graph:
      high_units: 100

然后仍运行 `.venv/bin/python main.py`。

`run.case_seed` 指定一个确切的用例种子，并使程序只执行一轮。批量实验时将它设为 `null`，由 `run.seed` 控制调度顺序和用例种子的生成。轮数、时长及保存全部用例分别由 `run.rounds`、`run.minutes` 和 `run.save_all` 设置；数据库地址使用 `database.uri`。

重放前在配置中指定用例目录：

    replay:
      case: results/<run>/candidate/<case>
      check_only: true

运行命令：

    .venv/bin/python replay.py

重放使用当前配置的数据库连接。`replay.check_only` 为 `false` 时，使用用例归档中的测量参数进行性能检查。

随机种子可能生成没有兼容位置的 AST，此时程序记录跳过，不修改外层查询来凑出目标模式。

## 从哪里阅读代码

先读 `main.py` 的 `prepare_case()`，再依次查看：

1. `query_generation/generator.py`：随机生成查询；不接收 Pattern 编号。
2. `query_generation/embedding.py`：选择兼容位置，检查查询对。
3. `mutator/`：各 Pattern 的源形式、局部图构造和变异。
4. `graph_generation/builder.py`：绑定符号、生成背景图、调用固定构图配方。
5. `databases/neo4j.py`：装载图、执行查询和获取计划。
6. `executor.py`：比较完整结果，计时、确认并保存。

`ast_nodes/` 表达语法结构；`data_structures/scope.py` 处理变量可见性。项目没有通用约束求解器、旧接口兼容层或多后端插件框架。

## 当前 Pattern

| 编号 | 含义 |
| --- | --- |
| S1 | 真条件下的 OR |
| S2 | 假条件下的 AND |
| S3 | CASE 未选结果或后续条件 |
| S4 | COALESCE 非空前缀 |
| S5 | 零输出需求 |
| S6 | 有限输出需求 |
| S7 | 存在性决定，包括否定存在性和联合见证 |
| S8 | 空输入传播 |
| Y1 | AND 操作数交换 |
| Y2 | OR 操作数交换 |
| Y3 | 单边书写方向反转 |
| Y4 | 有界路径端点反转 |
| Y5 | 独立必选匹配交换 |
| Y6 | 共享锚点必选分支交换 |
| Y7 | 链式连接交换 |
| Y8 | 独立可选分支交换 |

这些是新实现的编号。`BUG list/` 中的历史编号不变，不能直接按同名编号对应。

决定项候选池包含常量、比较、小区域计数、存在性检查，以及来源可追踪的外层节点属性。依赖数据的候选与其固定构图步骤共同实现。

## 构图与语义范围

生成器支持已实现语法内的随机子句、表达式、匹配拓扑和嵌套子查询。它不等于完整 Cypher 语法，也不对全部查询均匀采样。

随机小图提供背景数据。Pattern 构造器补齐局部结构，不反向求解全部外层过滤条件，因此不能保证每个随机用例都触及目标操作。

相关构图只选择具有可追踪标签来源的非空节点变量。构图器为候选锚点集合准备数据，不把变量误认为永久对应某个单独节点。

`high_units` 表示一个用例的高成本扩张单元总数，再分配给锚点或区域。一个路径单元可能包含多个节点和多条关系；背景图、小区域及固定见证另计。结果文件记录最终节点数与关系数。

同一个查询对使用同一张图。下一用例可以重建实验库。装载用的临时索引在测量前删除，正式测量只保留用例指定的索引。

路径反转不能改变外层观察到的路径顺序。程序不会通过把正反路径归一化来掩盖结果差异。

## 性能判定

默认参数：

- 随机背景图包含 50 个节点和 100 条关系。
- 高成本扩张单元总数为 100,000。
- 初筛每条查询预热 3 次，执行 6 对计时。
- 确认每条查询预热 5 次，执行 20 对计时。
- 耗时比至少为 **5 倍**，配对耗时差的中位数至少为 **100 ms**。
- 方向一致比例至少为 0.8。
- 单次查询超时为 10 秒。

两轮均交替执行 AB/BA，并要求慢查询方向保持一致。计时包含结果传输和完整读取，不包含建图、结果规范化或 PROFILE。

结果比较保留类型、重复行数量、实体身份，以及列表和路径内部顺序。结果不一致的用例不进入性能筛选。

通过计时阈值的用例是性能候选，仍需结合执行计划解释。超时单独记录，不把超时上限当成实际耗时。

## 输出与复现

每轮写入 `summary.csv`。性能候选、结果差异和超时会保存查询文本、实际图数据、配置和计时结果。完成计划采集后另存 `plans.json`。

`run.save_all: true` 可以保留普通用例。重放直接读取保存的图和查询，不依赖新版本代码按旧种子重新生成。

当前仅实现 Neo4j。迁移到其他系统时，还需要检查语法、结果语义和 Pattern 条件，不能只替换连接接口。

历史材料保存在 `paper/`、`BUG list/`、`reference/` 和 `picture/`，其中的方法说明不自动代表新实现。新版本的功能范围以当前代码和英文 README 为准。

## 已提交 issue 总表

以后统一维护根目录的 [bugs.csv](bugs.csv)，每个 GitHub issue URL 占一行。新提交增加一行，状态变化更新原行。`BUG list/` 保存历史报告，其中也有未提交的本地报告，不再维护另一份状态汇总表。

Pattern 编号采用提交 `26f62dc` 引入的 S1–S8、Y1–Y8，根据 issue 中的查询对和图条件判断，不能直接沿用历史文件夹编号。

- `pattern_ids` 和 `pattern_count` 记录直接语义对应的 Pattern 及其数量。对应语义不等于当前生成器能够原样生成该报告，也不代表已经实现相应数据库接口。
- `related_pattern_ids` 和 `related_pattern_count` 记录仅优化机制相关的 Pattern，单独统计。`mapping_notes` 说明区别；`mapping_class` 区分直接对应、仅机制相关和目录外问题。
- `status` 是当前处理状态，`reported_status` 保留登记时提供的原始状态。`status_basis`、`status_evidence` 和 `status_notes` 保存判断依据、回复链接及版本限定。
- `github_state` 单独记录是否关闭；关闭不自动等于修复。`upstream_type` 保留上游的问题类型，`bug_type` 保留提交者的性能或逻辑 BUG 分类。自动复现回复不视为人工确认。
- `duplicate_of` 记录上游合并关系，`last_checked` 记录核对日期。多个 Pattern ID 用分号分隔，空字段对应数量为零。

`uninterested` 表示上游明确不计划处理；`not_a_bug` 表示上游否认问题归属于该数据库。保留原始状态可以追踪本次核对带来的变化。

提交总数按 issue URL 去重；各 Pattern 数量按 `pattern_ids` 逐行统计，不计 `related_pattern_ids`。同一 issue 可以对应多个 Pattern，因此关联数和 issue 数需要分别报告。ArcadeDB #10307、#10308 已合并到 #10309，仍保留为两条提交记录；记录数不能直接作为独立根因数。

# GraphSymPerf

用于论文实验的图数据库性能缺陷检测代码。采用 SCor 风格的有限模板和候选池，构造低／高成本区域，测试结果保持的对称变体与低成本参考。当前模式为 P1–P16 与 SCor 图查询适配 S1–S10；只支持显式列出的受限结构，不是通用 Cypher 重写器。

## 文件

```text
Src/
  main.py        配置、实验预算、保存与重放
  generator.py   模板、候选绑定、受控构图、参考查询
  mutator.py     对称变换与高成本引用替换
  query.py       受限 AST、解析、渲染与作用域检查
  executor.py    Neo4j / Memgraph / FalkorDB 执行与重复测量
  summarize.py   独立 BUG 与 pattern 贡献汇总
config.yaml      单个测试目标和实验参数
bugs.csv         人工核查后的 BUG 对应表，初始为空
tests/          离线测试与显式启用的数据库集成测试
```

## 安装与运行

在仓库根目录执行：

```bash
uv sync --locked --extra test --extra falkordb
source .venv/bin/activate
export GRAPHSYMPERF_NEO4J_PASSWORD='你的测试库密码'
python Src/main.py --config config.yaml
```

先将配置指向**专用测试数据库**，显式设置 `dedicated: true` 和 `allow_reset: true`。默认配置关闭重建权限。Neo4j 和 Memgraph 每次构图清空指定测试库，拒绝未知业务索引或约束；FalkorDB 只重建配置的命名图，不清空 Redis 实例。密码仅通过配置指定的环境变量读取。一次只运行一个测试进程。

Neo4j 的连接配置见 `config.yaml`。Memgraph 可将其中的 `database` 替换为：

```yaml
database:
  dbms: memgraph
  uri: bolt://127.0.0.1:7687
  database: memgraph
  dedicated: true
  allow_reset: true
```

上述 Memgraph 示例用于未启用认证的本地专用实例；启用认证时再填写 `user` 和 `password_env`。FalkorDB 示例：

```yaml
database:
  dbms: falkordb
  uri: redis://127.0.0.1:6379
  database: gsp_paper_test
  dedicated: true
  allow_reset: true
```

FalkorDB 的 `database` 是独立图名；有认证时填写 `password_env`。服务器不能静默截断结果：执行器会检查 `RESULTSET_SIZE`，要求关闭截断或上限足够容纳本工具的结果限制。该检查不会修改 Redis 的全局配置。

每个数据库版本的功能支持以实际执行为准。不支持的模式或语法记录为 `unsupported_feature`，不能计为发现 BUG，也不能以改写成另一种操作来宣称实现了原机制。

## 模板与图状态

默认直接使用自有模板，`generation.log_files: []`，不依赖 GDsmith。配置日志路径可继续使用 GDsmith 作为可选结构输入；这不是保持 GDsmith 原查询结果的变换，等价性从模板实例化后的查询开始。

`seeds` 控制查询候选绑定；`generation.data_seed` 控制图数据布局。固定后者，同一 pattern 和规模的查询组合使用相同构图。候选池包含受控谓词写法、匹配与过滤位置、分支次序、有限路径界限、计数／存在操作和列表表达式。实际 `template_id`、`variant`、绑定和来源写入每个案例。增加种子只会枚举有限组合；不能把重复查询数当成覆盖数。

P 模式主要包括端点／路径镜像、布尔操作数交换、比较对偶、标签／类型顺序、MATCH 与独立 OPTIONAL 分支重排、UNION ALL 重排、CASE 对偶和 ALL 列表分组交换。保留 NULL、重复行、变量依赖、关系匹配作用域和输出列语义。

S 模式按 SCor 原论文十项分类对应 OR、AND、条件分支、CASE、跳过／限制扫描、COALESCE、首次匹配、空输入连接和空集合运算适配。S3 使用 Cypher 25 条件查询；S10 使用空输入与 EXISTS／NOT EXISTS 适配，不是原生 INTERSECT／EXCEPT。

S 模式以小参考区域替换高成本关系引用；LIMIT 和 EXISTS 保留足够见证。P4/P5/P15 增加由可见 `keep` 决定事实构造的参考关系，只省略已被决定项屏蔽的工作；P16 通过显式失败元素构造短列表参考。其他 P 模式继续比较对称查询对，不强行添加答案标签或低成本参考。P2 在同一状态中使用配对正／逆关系，不是整图物理反转实验。

## 实验预算与检测

`cases_per_pattern` 和 `seconds_per_pattern` 为每种模式保留独立预算；`schedule_seed` 固定模式顺序的随机排列。模式之间不转移未使用的预算。先按规模、再按查询种子执行，以便复用健康的已检查状态。每次确认候选仍重新连接和重建图。

旧配置的 `max_cases`、`max_seconds` 仍可读取，会分配到各模式；案例预算必须至少覆盖每种模式一次。预算在案例之间检查，已经开始的案例由查询超时约束，因此实际用时可能超过配额。每个运行的 `run.json` 保存实际尝试数、执行数、用时和停止原因。

先核验索引、成本条件、完整结果多重集及构造预期结果。参考查询存在时先执行参考；除对称 A/B 外，还比较 A/reference、B/reference，避免遗漏两种对称写法都慢的情况。

普通查询计时包含提交到完整消费结果，连接建立不计入查询耗时。初筛后使用两次重建状态的重复确认、平衡 AB/BA 顺序和 A/A、B/B 噪声对照。`candidate` 仅表示稳定性能差异；仍需核查执行计划、可避免工作、缩减案例和根因，才能登记为 BUG。

高成本查询超时但参考正确快速时记录 `timeout_candidate` 与保守下界，不伪造精确耗时。服务器明确确认终止且健康检查成功后才继续；取消状态不明时停止目标。超时候选不等同于完整结果已核验或性能差异已重复确认。

## 保存与重放

每次运行创建 `results/<运行>/`。每例包含 `setup.cypher`、`query_a.cypher`、`query_b.cypher`、可选的 `query_ref.cypher` 和 `result.json`，保存绑定、来源、预期结果、版本、全部样本和输入指纹。

```bash
python Src/main.py --config config.yaml --replay results/<运行>/<案例>
```

重放读取保存输入和测量参数，输出到新的目录。输入被修改时拒绝沿用旧指纹。旧版四文件案例继续可读。重放不重新调用生成器，也不覆盖原始证据。

## BUG 与 pattern 贡献

`bugs.csv` 初始只有表头，不预填历史或假定 BUG：

```text
case_id,dbms,bug_id,status,issue_url,database_version
```

`case_id` 是 `result.json` 的 `input_hash`；也接受至少 8 位且唯一的前缀。同根因不同案例使用同一 `bug_id`；一个 BUG 可由多个 pattern 命中，每个案例单独登记。不同 DBMS 的 BUG 分开计数。`database_version` 必须与实际记录一致；同一 BUG 跨版本仍使用同一 ID，但逐版本登记经过核查的案例。

状态为 `needs_review/reproduced/confirmed/fixed/rejected`。`reproduced` 指已经人工核查根因与证据的缺陷，不能只因为重跑得到性能差异就使用它。只有 `reproduced/confirmed/fixed` 的案例进入 BUG 贡献统计；开发者确认数量应另看 `confirmed/fixed`。

```bash
python Src/summarize.py --results results --bugs bugs.csv --output results/summary
```

输出 `summary.csv`（每系统、每 pattern 的有效案例、候选、独立 BUG、独有 BUG、用时与 BUG/小时）和 `pattern_bug.csv`（BUG × pattern 对应矩阵）。同系统同输入的重放去重，同根因跨规模／模板归并。缺少预算清单的旧结果不计算 BUG/小时；未执行或不支持的模式保留覆盖状态。

支持重复的 `--dbms` 与 `--version` 过滤。版本过滤仅计入该版本经过人工核查的对应案例，不把旧版本的确认自动归到新版。对比“仅 S”与“加入 P”时使用相同总预算；比较单个模式时使用相同模式预算。独有命中是观察结果，声称边际贡献还需移除该模式的消融实验。

## 测试

```bash
python -m pytest -m 'not integration'
# 仅在明确指定专用 Neo4j 时启用旧集成测试：
GRAPHSYMPERF_TEST_URI=bolt://127.0.0.1:37687 python -m pytest -m integration tests/test_neo4j.py tests/test_scor_neo4j.py
# 指定目录内须有 neo4j.yaml、memgraph.yaml、falkordb.yaml，连接均为独立测试库：
GRAPHSYMPERF_TEST_CONFIG_DIR=/absolute/test/configs python -m pytest -m integration tests/test_multi_dbms.py
```

集成测试真实执行构图、检查、测量和保存输入；通过不代表发现新 BUG。正式缺陷检测应增加图规模与查询组合预算，并保存完整环境和原始证据。

## 来源与历史

模板实现逻辑参考 Lai 等的 [SCor](https://doi.org/10.1145/3802061)。S1–S10 是已有机制的图查询适配，不能作为十项新贡献。实现保留结果检查与重复确认，不照搬发布包的单次异常判据。

旧框架与已提交实验材料可从 Git 提交 `a916479c7d600f672b3db1679de63398549db3ed` 查看。本机额外历史归档位于 `../GraphSymPerf-archive/20260929-130411-workspace-cleanup/`；当前运行不依赖该目录。

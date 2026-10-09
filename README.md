# GraphSymPerf

用于论文实验的图数据库性能缺陷检测代码。采用 SCor 风格的有限模板和候选池，构造低／高成本区域，测试结果保持的对称变体与低成本参考。当前模式为 P1–P16 与 SCor 图查询适配 S1–S10；只支持显式列出的受限结构，不是通用 Cypher 重写器。

## 文件

```text
Src/
  main.py        配置、实验预算、保存与重放
  generator.py   模板、候选绑定、受控构图、参考查询
  mutator.py     对称变换与高成本引用替换
  ir.py          语言无关的受限查询结构、验证和序列化
  query.py       受限 Cypher 解析、渲染与旧接口兼容
  languages.py   目标方言生成、输出列映射与能力检查
  state.py       有限图状态描述、分批数据与成本检查
  executor.py    六种数据库执行、原生计划与重复测量
  summarize.py   独立 BUG 与 pattern 贡献汇总
config.yaml      单个测试目标和实验参数
bugs.csv         人工核查后的 BUG 对应表，初始为空
tests/          离线测试与显式启用的数据库集成测试
```

## 安装与运行

在仓库根目录执行：

```bash
uv sync --locked --extra test --extra falkordb --extra age --extra ladybug
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

新增目标示例在 `configs/arcadedb.yaml`、`configs/age.yaml`、`configs/ladybug.yaml`。先准备专用测试目标，填写连接与密码环境变量，再显式开启 `allow_reset`。ArcadeDB 需要预先建立指定数据库；AGE 需要在指定 PostgreSQL 库安装 AGE 扩展，`graph` 仅指定本工具重建的图；Ladybug 使用指定目录中自己创建的临时子目录，每次独立连接都重新构图。嵌入式图不会被错误地跨连接复用。

ArcadeDB 使用原生 `opencypher` 前端；框架中的语言名仍为 `cypher`。AGE 使用原生 Cypher 包装及 PostgreSQL 计划，成本入口分别验证属性映射 GIN 和属性表达式 BTree；建图后对实验表执行 `VACUUM (ANALYZE)`，清理 GIN 待处理项并刷新统计。Ladybug 使用类型化节点／关系表及主键索引，路径显式指定原生 `TRAIL`。它的多标签语义不能用于 P7。每个目标的已验证版本和支持范围见 [Cypher 实测覆盖](docs/cypher-support.md)。

骨架和成本角色直接使用结构化数据，内置模板不依赖解析虚构的 Cypher 种子。流程是骨架 → `GraphSpec` → 状态绑定 → 查询 IR → 语义保持变换 → 目标方言。图状态按有限 recipe 分批构造，成本数量、属性、逆关系和参考区域在真实目标中检查。解析器仅负责可选 Cypher 种子导入，公共 IR 不依赖语言渲染器；生成时保留布尔项、标签、路径和分支顺序。

Gremlin 与 GQL 已预留同一个语言生成接口，目前明确返回 `UnsupportedLanguage`，尚无执行实现。`languages.generate()` 支持经过目标版本验证的 `verified_features` 覆盖保守语法限制；它不能覆盖已知的语义不兼容。不同类型的 COALESCE、路径重复规则和可选匹配的 NULL 延展必须在目标语言中另行验证。

## 模板与图状态

默认直接使用自有模板，`generation.log_files: []`，不依赖 GDsmith。配置日志路径可继续使用 GDsmith 作为可选结构输入；这不是保持 GDsmith 原查询结果的变换，等价性从模板实例化后的查询开始。

`seeds` 控制查询候选绑定；`generation.data_seed` 控制图中噪声节点的标识偏移，当前不随机改变拓扑。固定后者，同一 pattern、状态变体和规模的查询组合使用相同构图。候选池包含受控谓词写法、匹配与过滤位置、分支次序、有限路径界限、计数／存在操作和列表表达式。实际 `template_id`、`variant`、绑定和来源写入每个案例。增加种子只会枚举有限组合；不能把重复查询数当成覆盖数。

`generation.complexity` 可设为 0、1、2。0 保留原有限枚举序列；1/2 对每个角色独立、可复现地抽样，并增加一层／两层布尔、CASE、COALESCE 嵌套和关联条件。SCor 决定项可读取受控根节点属性；S3 使用廉价单根节点计数。动态决定事实的值与根节点唯一性必须在数据库中检查，不能只相信生成器的元数据。复杂度扩展的是受控查询结构，不是任意 Cypher 文法。

P 模式主要包括端点／路径镜像、布尔操作数交换、比较对偶、标签／类型顺序、MATCH 与独立 OPTIONAL 分支重排、UNION ALL 重排、CASE 对偶和 ALL 列表分组交换。保留 NULL、重复行、变量依赖、关系匹配作用域和输出列语义。

`generation.state_variant` 默认 `original`。定向实验可设置 `empty_low`（仅 P10/P11），让受测低成本分支为零匹配，但保留高成本分支和其成功见证；P11 在另一根节点保留 LOW 边，避免全局缺失关系类型使空结果直接折叠。`multi_low`（仅 P11/P13）为受测根节点提供八个低侧匹配，检验分支重排是否导致高侧重复扫描；完整结果及 OPTIONAL 的 NULL 延展都单独检查。配置的 patterns 必须与该状态变体兼容。

`pruning_path`（仅 P3）构造低端入口、`keep=false` 的失败节点及后续扇出，另保留一条全 `keep=true` 的两跳见证；查询使用固定 `1..3` 界限和 `ALL(x IN nodes(p) WHERE x.keep=true)`。路径镜像保留完整节点谓词与结果多重集。当前只允许 `nodes(p)` 作为 ALL 的直接输入，谓词观察绑定节点自身的标量属性；不允许返回图实体或观察路径位置。

S 模式按 SCor 原论文十项分类对应 OR、AND、条件分支、CASE、跳过／限制扫描、COALESCE、首次匹配、空输入连接和空集合运算适配。S3 使用 Cypher 25 条件查询；S10 使用空输入与 EXISTS／NOT EXISTS 适配，不是原生 INTERSECT／EXCEPT。

S 模式以小参考区域替换高成本关系引用；LIMIT 和 EXISTS 保留足够见证。P4/P5/P15 增加由可见 `keep` 决定事实构造的参考关系，只省略已被决定项屏蔽的工作；P16 通过显式失败元素构造短列表参考。其他 P 模式继续比较对称查询对，不强行添加答案标签或低成本参考。P2 在同一状态中使用配对正／逆关系，不是整图物理反转实验。

## 实验预算与检测

`cases_per_pattern` 和 `seconds_per_pattern` 为每种模式保留独立预算；`schedule_seed` 固定模式顺序的随机排列。模式之间不转移未使用的预算。先按规模、再按查询种子执行，以便复用健康的已检查状态。每次确认候选仍重新连接和重建图。

旧配置的 `max_cases`、`max_seconds` 仍可读取，会分配到各模式；案例预算必须至少覆盖每种模式一次。预算在案例之间检查，已经开始的案例由查询超时约束，因此实际用时可能超过配额。每个运行的 `run.json` 保存实际尝试数、执行数、用时和停止原因。

先核验索引、成本条件、完整结果多重集及构造预期结果。参考查询存在时先执行参考；除对称 A/B 外，还比较 A/reference、B/reference，避免遗漏两种对称写法都慢的情况。

普通查询计时包含提交到完整消费结果，连接建立不计入查询耗时。初筛后使用两次重建状态的重复确认、平衡 AB/BA 顺序和 A/A、B/B 噪声对照。`candidate` 仅表示稳定性能差异；仍需核查执行计划、可避免工作、缩减案例和根因，才能登记为 BUG。

`measurement.collect_plans: true` 在计时结束后保存原生 PROFILE；性能候选默认采集计划。计划失败会单独记录，不能代替结果与性能检查。各引擎的 DB Hits、算子输出数量、loops 和缓冲区统计保留原名，不转换成一个虚假的统一工作量指标。

每个比较独立保存 `pair_kind`、`status` 和测量摘要：`comparisons.ab` 保存原始 A/B 证据；`reference_comparisons` 保存 A/reference、B/reference 样本。顶层 `candidate_comparison` 仅标出差异最大的已通过比较，不能用它判断 A/B 是否命中。SCor 的 HIGH/REF 属于参考比较。P16 把失败元素从列表头移到尾，普通顺序 ALL 求值会增加谓词检查次数；这种预期工作差异不能直接登记为遗漏优化。

Neo4j 先分批删除关系、再分批删除节点，避免密集图清理占满单次事务内存。每次重建图并等待索引后，调用 `db.prepareForReplanning()` 更新统计并清理查询缓存；这一步不计入被测查询耗时。它避免把前一图状态的陈旧计划误算为新性能问题。

高成本查询超时但参考正确快速时记录 `timeout_candidate` 与保守下界，不伪造精确耗时。服务器明确确认终止且健康检查成功后才继续；取消状态不明时停止目标。超时候选不等同于完整结果已核验或性能差异已重复确认。

## 保存与重放

每次运行创建 `results/<运行>/`。每例包含 `setup.cypher`、`query_a.cypher`、`query_b.cypher`、可选的 `query_ref.cypher` 和 `result.json`，保存绑定、来源、预期结果、版本、全部样本和输入指纹。

新格式 3 在 `result.json` 中额外保存查询 IR、图状态描述、实际方言、输出列映射与逻辑 `ir_hash`。`setup.cypher` 保留传统构图展示，新适配器以 `graph_spec` 分批构图；它不是 AGE/Ladybug 的可直接执行建图脚本。新库的 `query_*.cypher` 是实际执行文本。格式 1/2 的原 `input_hash` 保持兼容；旧格式携带新增执行元数据时，另用 `extension_hash` 校验。跨目标重放使用已保存 IR 重新生成，不重新调用随机生成器。

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

输出 `summary.csv`（每系统、每 pattern 的有效案例、候选、独立 BUG、独有 BUG、用时与 BUG/小时）和 `pattern_bug.csv`（BUG × pattern 对应矩阵）。`symmetry_candidates` 与 `reference_candidates` 分别统计独立比较的命中，每案例每类最多计一次，两列可重叠。历史结果缺少独立 AB 状态时，候选还记入 `unclassified_candidates`，该列也可与明确的参考候选重叠。不能从历史顶层 winner 反推对称候选。同系统同输入的重放去重，同根因跨规模／模板归并。缺少预算清单的旧结果不计算 BUG/小时；未执行或不支持的模式保留覆盖状态。

支持重复的 `--dbms` 与 `--version` 过滤。版本过滤仅计入该版本经过人工核查的对应案例，不把旧版本的确认自动归到新版。对比“仅 S”与“加入 P”时使用相同总预算；比较单个模式时使用相同模式预算。独有命中是观察结果，声称边际贡献还需移除该模式的消融实验。

新增可重复的 `--language` 过滤，旧记录默认 `cypher`。`summary.csv` 按数据库 × 语言 × Pattern 分组；`exclusive_bugs` 表示该语言内所选 Pattern 的独占命中。`pattern_bug.csv` 仍按 `(dbms, bug_id)` 跨语言去重，`bugs.csv` 保持六列；未经核验的语言案例不能借用其他语言的确认。

汇总读取所选目录内 `EXCLUDED.json` 的显式标记：`include_in_analysis: false` 排除对应范围的结果及运行清单。无效清单报错，不根据目录名猜测。语义冒烟测试、受干扰的轮次及不同计时口径的旧结果应单独保存并标记，不能混进正式贡献统计。

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

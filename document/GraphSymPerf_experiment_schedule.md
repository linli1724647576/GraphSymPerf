# GraphSymPerf 全流程实验日程与执行方案

本文档用于指导 GraphSymPerf 在服务器上的全量实验。实验主线只保留两类语义等价变换：

1. `bool_redundancy`：GraphBoolPerf 当前主线，面向冗余布尔谓词、恒真/恒假分支、NULL 矛盾、双重否定等语义保持变换。
2. `symmetry`：GraphSymPerf 当前主线，面向谓词顺序、图模式顺序、标签顺序、属性顺序、变量重命名等语义保持对称变换。

其他能力不作为新的研究主线，而作为实验支撑机制：语义有效与非空查询生成、Plan Metamorphic Coverage、反馈引导、候选归类与去重、replay benchmark、分层验证、实验分片与聚合。

---

## 0. 实验总目标

本实验的目标不是简单跑出大量候选，而是系统性回答以下研究问题：

| 编号 | 研究问题 | 需要的实验支撑 |
|---|---|---|
| RQ1 | GraphSymPerf 是否能在真实图数据库中发现 previously unknown performance anomaly candidates？ | Main effectiveness run、triage、validation、issue reporting |
| RQ2 | `bool_redundancy` 和 `symmetry` 分别贡献了哪些异常和覆盖？ | Transform-family ablation、rule-level statistics、PMC summary |
| RQ3 | Plan Metamorphic Coverage 是否能衡量等价变换探索到的优化器行为？ | PMC growth、PMC by family/rule、PMC vs candidate density |
| RQ4 | 反馈引导是否比静态规则选择更高效？ | Static vs feedback ablation |
| RQ5 | 分层 oracle 是否比 runtime-only 或 plan-only 更可靠？ | Oracle reliability validation |
| RQ6 | 候选能否被归类、去重并形成可复现的 unique issue candidates？ | Candidate clustering、signature、known issue replay、triage |
| RQ7 | 已发现问题是否能稳定复现并转化为开发者可接受的 issue？ | Replay benchmark、reducer、runtime validation、issue draft |

论文中最终使用的核心结果不是 raw candidates，而是：

```text
Equivalent cases
Profile-supported candidates
Runtime-supported candidates
Issue families
Unique issue candidates
Reported issues
Confirmed / fixed / wont-fix / upstream cases
PMC units and growth curves
Root-cause taxonomy
```

---

## 1. 总体实验流水线

服务器全量实验按以下顺序执行：

```text
代码冻结与本地测试
  -> 服务器 Doctor / Preflight
  -> Known Issue Replay Sanity Check
  -> Calibration Run
  -> Main Effectiveness Run
  -> Ablation Runs
  -> Oracle Reliability Validation
  -> Candidate Triage and Reduction
  -> Issue Reporting and Developer Feedback Tracking
  -> Final Aggregation and Paper Table Export
```

每一步都有明确的目的、判定门槛和产出。不要跳过前置阶段直接跑主实验，否则后续数据很可能不可用或难以解释。

---

## 2. 实验前代码能力检查

### 2.1 实验目的

确保代码已经具备服务器全量实验需要的基本能力，避免主实验阶段才发现 artifact 缺失、结果不可聚合或候选无法去重。

### 2.2 为论文哪部分提供论据

这一阶段本身通常不直接进入论文结果表，但它是所有后续实验可复现性的前提。可以在 artifact/evaluation setup 中简要说明工具支持 replay、sharding、resume、aggregation、triage。

### 2.3 必须具备的能力

代码上传服务器前应具备以下能力：

```text
1. bool_redundancy 和 symmetry 都是 TransformFamily。
2. 每个等价 case 能生成 PlanDiff。
3. 每个等价 case 能生成 PMCEvidence。
4. 每个候选能生成 CandidateSignature。
5. 能输出 clustered_summary、unique_issues、issue_families。
6. 能按 YAML manifest 运行实验。
7. 能按 shard 分片运行。
8. 能 resume，不重复已完成 case。
9. 能 aggregate 多个 shard。
10. 能生成 triage top representatives。
11. 能对 top representatives 做 replay/profile/runtime validation。
12. 已有 known issue cases 能被 replay。
```

### 2.4 本地检查方法

本地执行：

```bash
python -m pytest

graph-sym-perf --help

graph-sym-perf doctor \
  --config config/experiments/calibration.yaml \
  --dry-run

graph-sym-perf replay \
  --all-known \
  --config config/experiments/replay_known.yaml \
  --dry-run
```

### 2.5 预期结果

```text
pytest 全部通过；需要数据库的测试在缺少环境变量时 skip。
CLI 能显示所有实验命令。
doctor dry-run 能解析配置。
replay dry-run 能枚举 known issue cases。
```

### 2.6 产出

```text
本地 pytest log
本地 dry-run log
当前 git commit / 代码版本号
配置文件版本
```

### 2.7 通过门槛

只有满足以下条件才上传服务器：

```text
1. python -m pytest 通过。
2. config/experiments/*.yaml 均能解析。
3. known issue manifest 能读取。
4. artifact schema 包含 schema_version。
5. PMC / feedback / clustering 模块有单元测试。
```

---

## 3. 服务器 Doctor / Preflight

### 3.1 实验目的

检查服务器环境是否满足全量实验要求，包括数据库连接、PROFILE/EXPLAIN 能力、输出目录权限、磁盘空间、系统版本和配置一致性。

这一阶段用于避免主实验中途失败，尤其是多数据库、多 shard 场景下，任何一个 adapter 或 profile parser 异常都会影响整体结果。

### 3.2 为论文哪部分提供论据

支持论文的实验设置可信度。可以用于说明：

```text
每个 DBMS 的版本、Docker image digest、系统环境、adapter capability、profile metric availability。
```

这些信息会进入实验设置表。

### 3.3 实现方法

在服务器上执行：

```bash
graph-sym-perf doctor \
  --config config/experiments/main_effectiveness.yaml
```

如果希望严格检查所有系统：

```bash
graph-sym-perf doctor \
  --config config/experiments/main_effectiveness.yaml \
  --strict
```

Doctor 应检查：

```text
1. YAML manifest 可解析。
2. 输出目录可写。
3. 每个系统的环境变量是否存在。
4. adapter factory 能否创建对应 adapter。
5. 数据库连接是否可用。
6. execute 是否可用。
7. explain/profile 是否符合 capabilities。
8. 版本信息是否能记录。
9. 磁盘空间是否充足。
10. 当前 git commit / tool version 是否记录。
```

### 3.4 预期结果

```text
所有目标系统 connection ok。
每个系统 capabilities 明确。
缺少某些 metric 的系统被标记，而不是后续误当作 0。
输出目录可写。
磁盘空间足够。
```

### 3.5 产出

```text
runs/preflight/preflight.json
runs/preflight/preflight.md
server_environment.json
engine_versions.json
adapter_capabilities.json
```

### 3.6 判定门槛

若出现以下情况，应暂停主实验：

```text
1. 数据库无法连接。
2. PROFILE/EXPLAIN 解析完全失败。
3. 输出目录不可写。
4. 磁盘空间不足。
5. 系统版本无法记录。
```

若某系统只缺少部分指标，例如没有 DB hits，但有 rows 或 records produced，可以继续实验，但在论文中必须说明该系统的 metric availability。

---

## 4. Known Issue Replay Sanity Check

### 4.1 实验目的

验证当前服务器环境、adapter、profile parser、PlanDiff、PMC、candidate signature 和 replay 机制是否能复现已有案例。

这一步不是为了发现新 issue，而是为了证明实验平台端到端可用。

### 4.2 为论文哪部分提供论据

支持以下部分：

```text
1. 可复现性论证。
2. 已知案例 benchmark。
3. Developer feedback taxonomy：fixed / wont-fix / pending / upstream。
4. 工具 artifact 的稳定性。
```

### 4.3 实现方法

执行：

```bash
graph-sym-perf replay \
  --all-known \
  --config config/experiments/replay_known.yaml
```

如果按系统分开：

```bash
graph-sym-perf replay --case neo4j_13783_fixed

graph-sym-perf replay --case arcadedb_4405_fixed
```

每个 known case 应验证：

```text
1. graph setup 可加载。
2. base query 可执行。
3. transformed query 可执行。
4. result equivalence 成立。
5. plan/profile symptom 是否仍存在。
6. PMCEvidence 是否能提取。
7. CandidateSignature 是否稳定。
8. developer_status 是否正确记录。
```

### 4.4 预期结果

```text
fixed case：旧版本可复现；如果测试新版本，可能 symptom 消失，应记录为 fixed behavior。
wont-fix case：现象可能仍存在，但 developer_status = wont_fix。
pending case：若仍存在，作为后续回归和对照。
upstream case：记录根因归属，不计作本系统 confirmed bug。
```

### 4.5 产出

```text
runs/replay_known/replay_summary.json
runs/replay_known/replay_summary.md
runs/replay_known/cases/<case_id>/pmc_evidence.json
runs/replay_known/cases/<case_id>/candidate_signature.json
runs/replay_known/cases/<case_id>/profile_base.txt
runs/replay_known/cases/<case_id>/profile_transformed.txt
```

### 4.6 判定门槛

继续主实验前应满足：

```text
1. 至少 Neo4j 或一个主要系统的 known case 能完整 replay。
2. PMC evidence 能生成。
3. clustered summary 能识别 known issue 的 root-cause label。
4. replay 失败原因可解释，例如系统版本已修复，而不是工具崩溃。
```

---

## 5. Calibration Run

### 5.1 实验目的

小规模校准每个目标系统的有效查询率、结果等价率、非空率、PROFILE 可用率、timeout 比例、runtime 噪声和 PMC 增长情况。

Calibration 的作用是确定主实验参数，而不是发现大量问题。

### 5.2 为论文哪部分提供论据

支持实验设置和威胁分析：

```text
1. 各系统 metric availability。
2. 阈值选择理由。
3. timeout 和 runtime repeat 的合理性。
4. query generation 的语义有效性与非空率。
```

### 5.3 实现方法

执行：

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/calibration.yaml
```

建议配置：

```text
每个系统 100~500 seed cases。
feedback.enabled = false。
runtime validation disabled 或 very light。
coverage.enabled = true。
clustering.enabled = true。
```

推荐初始参数：

```yaml
execution:
  shards: 1
  cases_per_shard: 200
  per_query_timeout_seconds: 30
  per_case_timeout_seconds: 120

feedback:
  enabled: false

coverage:
  enabled: true

oracle:
  runtime:
    mode: disabled
```

### 5.4 需要观察的指标

```text
1. valid query rate
2. result equivalence rate
3. non-empty result rate
4. profile availability rate
5. timeout rate
6. average time per case
7. PMCEvidence generation rate
8. PMC new units / 100 cases
9. raw candidate rate
10. candidate duplicate rate
```

### 5.5 预期结果

理想情况下：

```text
valid query rate 不低于 50%。
result equivalence rate 对 strict rules 应较高。
profile availability 对主要系统可用。
timeout rate 低于 10%~20%。
PMC 在前 100~500 cases 中持续增长。
```

如果结果不理想：

```text
valid query rate 低：修 query generation / dialect rendering。
equivalence rate 低：检查 transformation soundness。
profile availability 低：修 profile parser 或 adapter capabilities。
timeout 高：缩小 graph/query size，降低 max variants，调 timeout。
PMC 不增长：调整 enabled rules 或加入 feedback。
```

### 5.6 产出

```text
runs/calibration/summary.json
runs/calibration/summary.md
runs/calibration/pmc_summary.json
runs/calibration/pmc_summary.md
runs/calibration/clustered_summary.json
runs/calibration/rule_stats.json
runs/calibration/calibration_decisions.md
```

### 5.7 判定门槛

只有当 calibration 通过后才进入主实验。建议门槛：

```text
1. 每个主要系统至少能完成 100 cases。
2. 工具不崩溃，失败都记录为 skip/error reason。
3. profile parser 不产生明显错误字段。
4. PMC summary 有有效 coverage units。
5. candidate clustering 能正常输出。
```

---

## 6. Main Effectiveness Run

### 6.1 实验目的

这是寻找 previously unknown performance anomaly issues 的主实验。目标是在多个真实图数据库系统中，大规模生成语义等价查询对/查询族，通过分层 oracle 和 PMC/feedback/clustering 找到高质量 unique issue candidates。

### 6.2 为论文哪部分提供论据

主要支持 RQ1：

```text
GraphSymPerf 是否能发现真实系统中的性能异常候选？
```

同时支持：

```text
RQ2：两个 transform family 的贡献。
RQ3：PMC 覆盖情况。
RQ6：unique issue candidates 和 root-cause taxonomy。
```

### 6.3 实现方法

推荐按系统和 shard 分开运行：

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/main_effectiveness.yaml \
  --shard 0 \
  --num-shards 16

graph-sym-perf experiment run \
  --manifest config/experiments/main_effectiveness.yaml \
  --shard 1 \
  --num-shards 16

# ...

graph-sym-perf experiment run \
  --manifest config/experiments/main_effectiveness.yaml \
  --shard 15 \
  --num-shards 16
```

也可以按系统拆分：

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/main_effectiveness.yaml \
  --systems neo4j \
  --shard 0 \
  --num-shards 8
```

主实验建议配置：

```text
transform_families = [bool_redundancy, symmetry]
feedback.enabled = true
coverage.enabled = true
clustering.enabled = true
runtime.mode = selective
reducer.enabled = selective
include_exploratory = false 或单独开实验
```

### 6.4 推荐规模

初始主实验：

```text
每系统 10,000 seed cases。
```

论文级主实验：

```text
每系统 20,000~50,000 seed cases。
```

如果资源充足：

```text
每系统 100,000 seed cases。
```

建议先跑较小规模确认稳定，再扩大。

### 6.5 预期结果

主实验应产生：

```text
1. 大量结果等价 case。
2. 持续增长的 PMC units。
3. 若干 profile-supported candidates。
4. 更少数量的 runtime-supported candidates。
5. 经聚类去重后的 unique issue candidates。
6. 每个系统的 root-cause distribution。
7. 每个 transform rule 的 candidate contribution。
```

候选 funnel 预期形态：

```text
Generated cases
  > Valid cases
  > Equivalent cases
  > Plan/profile anomaly candidates
  > Profile-supported candidates
  > Runtime-supported candidates
  > Unique issue candidates
  > Reported issues
  > Confirmed/fixed issues
```

### 6.6 产出

每个 shard：

```text
runs/main_effectiveness/shard_000/summary.json
runs/main_effectiveness/shard_000/pmc_summary.json
runs/main_effectiveness/shard_000/feedback_summary.json
runs/main_effectiveness/shard_000/clustered_summary.json
runs/main_effectiveness/shard_000/candidate_signatures.jsonl
runs/main_effectiveness/shard_000/unique_issues.json
```

聚合后：

```text
runs/main_effectiveness/aggregate_summary.json
runs/main_effectiveness/aggregate_summary.md
runs/main_effectiveness/aggregate_pmc_summary.json
runs/main_effectiveness/aggregate_feedback_summary.json
runs/main_effectiveness/aggregate_clustered_summary.json
runs/main_effectiveness/aggregate_unique_issues.json
runs/main_effectiveness/aggregate_issue_families.json
runs/main_effectiveness/tables/*.csv
```

### 6.7 判定门槛

如果主实验中出现以下情况，应暂停并回到 calibration：

```text
1. timeout rate 长期超过 30%。
2. result equivalence rate 极低。
3. PMC 在早期后完全不增长。
4. raw candidates 很多但 unique issue 极少，重复率异常高。
5. profile-supported candidates 几乎为 0，说明 threshold 或 parser 可能有问题。
```

---

## 7. Aggregation After Main Run

### 7.1 实验目的

合并多个 shard 的结果，得到全局统计、去重后的 unique issues、全局 PMC 覆盖、全局 feedback rule stats。

### 7.2 为论文哪部分提供论据

直接产生论文主表：

```text
系统级有效性表
transform family 贡献表
PMC 覆盖表
candidate clustering 表
root-cause taxonomy 表
```

### 7.3 实现方法

```bash
graph-sym-perf experiment aggregate \
  --run-root runs/main_effectiveness_2026_01
```

Aggregation 应：

```text
1. 合并 summary。
2. 跨 shard 去重 PMC units。
3. 跨 shard 去重 candidate signatures。
4. 聚合 issue_family_key。
5. 聚合 unique_issue_key。
6. 聚合 rule_stats。
7. 输出 per-system 和 overall 统计。
8. 生成 CSV 表格。
```

### 7.4 预期结果

```text
aggregate_summary 能显示每个系统的完整漏斗统计。
aggregate_pmc_summary 能显示 PMC by family/rule/kind。
aggregate_unique_issues 能给出可 triage 的候选列表。
```

### 7.5 产出

```text
aggregate_summary.json / md
aggregate_pmc_summary.json / md
aggregate_feedback_summary.json / md
aggregate_clustered_summary.json / md
aggregate_candidate_signatures.jsonl
aggregate_unique_issues.json
aggregate_issue_families.json
tables/effectiveness.csv
tables/transform_contribution.csv
tables/pmc_by_rule.csv
tables/root_cause_distribution.csv
tables/candidate_funnel.csv
```

---

## 8. Transform-Family Ablation Runs

### 8.1 实验目的

评估 `bool_redundancy` 和 `symmetry` 分别贡献了什么，并证明二者放在统一框架中是有意义的。

### 8.2 为论文哪部分提供论据

支持 RQ2：

```text
不同等价变换族能暴露不同类型的优化器性能异常。
```

也支持 RQ3：

```text
不同变换族产生不同类型的 Plan Metamorphic Coverage。
```

### 8.3 实验配置

至少运行四组：

```text
1. bool_only_static
2. symmetry_only_static
3. both_static
4. both_feedback
```

如果资源允许，增加：

```text
5. bool_only_feedback
6. symmetry_only_feedback
7. both_feedback_without_pmc_reward
8. both_feedback_with_pmc_reward
```

### 8.4 实现方法

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/ablation_bool_static.yaml

graph-sym-perf experiment run \
  --manifest config/experiments/ablation_symmetry_static.yaml

graph-sym-perf experiment run \
  --manifest config/experiments/ablation_both_static.yaml

graph-sym-perf experiment run \
  --manifest config/experiments/ablation_both_feedback.yaml
```

每组实验应尽量使用：

```text
相同系统
相同 seed
相同 source
相同 case budget
相同 timeout
相同 profile thresholds
```

每组预算可以是主实验的 20%~30%。例如主实验每系统 50k，则 ablation 每系统 10k。

### 8.5 预期结果

预期观察：

```text
bool_redundancy 更容易产生 PredicateResidueIntroduced、PredicatePushdownBlocked、BooleanSimplificationMissing、NullContradictionSimplificationMissing。
symmetry 更容易产生 PatternOrderSensitivity、PredicateOrderSensitivity、AccessPointInstability、JoinOrderInstability。
both_feedback 在同等预算下产生更多 PMC new units 和更多 unique issue candidates。
```

### 8.6 产出

```text
runs/ablation_bool_static/aggregate_summary.json
runs/ablation_symmetry_static/aggregate_summary.json
runs/ablation_both_static/aggregate_summary.json
runs/ablation_both_feedback/aggregate_summary.json

tables/ablation_comparison.csv
figures/pmc_growth_by_strategy.csv
figures/candidate_density_by_strategy.csv
```

### 8.7 论文表格建议

| Strategy | Equivalent Rate | PMC Units / 1k Cases | Profile Candidates / 1k Cases | Unique Issues / 1k Cases | Duplicate Rate |
|---|---:|---:|---:|---:|---:|
| bool_only_static | | | | | |
| symmetry_only_static | | | | | |
| both_static | | | | | |
| both_feedback | | | | | |

---

## 9. Feedback Guidance Evaluation

### 9.1 实验目的

评估反馈引导是否能比静态规则选择更快探索新的计划差异，并产生更多高质量候选。

### 9.2 为论文哪部分提供论据

支持 RQ4：

```text
Plan feedback + PMC novelty reward 能提高等价变换选择效率。
```

### 9.3 实现方法

对比：

```text
both_static：feedback.enabled = false
both_feedback：feedback.enabled = true
```

可选进一步对比：

```text
both_feedback_without_pmc_reward
both_feedback_with_pmc_reward
```

### 9.4 评价指标

```text
1. PMC new units / 1000 cases
2. Unique plan delta / 1000 cases
3. Profile-supported candidates / 1000 cases
4. Unique issue candidates / 1000 cases
5. Duplicate rate
6. Timeout / invalid / equivalence failure rate
7. Rule reward convergence
```

### 9.5 预期结果

```text
feedback 策略在相同 case budget 下，PMC new units 更多，重复率更低，profile-supported candidates 或 unique issues 更多。
```

如果没有达到预期，也可以在论文中诚实分析：feedback 对某些系统有效，对某些系统不明显；这依然是合理结果。

### 9.6 产出

```text
feedback_summary.json
rule_stats.json
feedback_rule_reward_curve.csv
pmc_growth_static_vs_feedback.csv
candidate_density_static_vs_feedback.csv
```

---

## 10. Plan Metamorphic Coverage Evaluation

### 10.1 实验目的

验证 Plan Metamorphic Coverage 是否能够作为黑盒图数据库性能测试的覆盖度指标，衡量等价查询变换探索到的优化器行为差异。

### 10.2 为论文哪部分提供论据

支持 RQ3：

```text
PMC 能描述 bool_redundancy 和 symmetry 探索到的 plan-level behavior，并可用于反馈引导和实验评估。
```

### 10.3 实现方法

从主实验和消融实验中提取：

```text
PMC units by kind
PMC units by transform family
PMC units by transform rule
PMC growth over time
PMC new units per 1000 cases
candidate-producing PMC units
```

### 10.4 预期结果

```text
bool_redundancy 和 symmetry 的 PMC 分布不同。
反馈引导会提高 PMC 增长效率。
PMC 高增长阶段往往对应更多 profile-supported candidates。
```

### 10.5 产出

```text
pmc_summary.json
pmc_summary.md
pmc_by_family.csv
pmc_by_rule.csv
pmc_by_kind.csv
pmc_growth_curve.csv
candidate_pmc_correlation.csv
```

### 10.6 论文图表建议

```text
Figure: PMC growth over generated cases。
Figure: PMC unit distribution by transform family。
Table: Top PMC units and representative cases。
```

---

## 11. Oracle Reliability Validation

### 11.1 实验目的

评估分层 oracle 的可靠性，证明仅依赖 runtime、plan 或 estimated rows 都不够稳健；组合 result equivalence、plan diff、profile metric 和 runtime validation 更适合性能异常检测。

### 11.2 为论文哪部分提供论据

支持 RQ5：

```text
分层 oracle 能减少 runtime noise 和 estimated-only anomaly 带来的误判。
```

### 11.3 实现方法

从主实验的 unique issues 中抽样 top representatives：

```bash
graph-sym-perf validate \
  --triage runs/main_effectiveness/triage_top100.json \
  --runtime-repeats 15
```

或者使用 manifest：

```bash
graph-sym-perf experiment run \
  --manifest config/experiments/oracle_reliability.yaml
```

候选分层：

```text
plan-only
estimated-only
profile-supported
latency-supported
profile-and-latency-supported
```

对每类候选做 repeated runtime validation。

建议 runtime 配置：

```yaml
runtime:
  warmup: 3
  repeats: 15
  timeout_seconds: 120
  median_ratio_threshold: 2.0
  absolute_delta_ms: 50
  max_iqr_ratio: 0.5
```

### 11.4 预期结果

```text
runtime-only 可能存在噪声。
plan-only 中部分没有稳定 slowdown。
profile-supported 更容易通过 runtime validation。
estimated-only 需要单独标注，不能直接称为 runtime regression。
combined oracle 的候选质量最高。
```

### 11.5 产出

```text
oracle_reliability_summary.json
oracle_reliability_summary.md
runtime_validation_results.jsonl
runtime_distribution.csv
oracle_level_comparison.csv
```

### 11.6 论文表格建议

| Oracle Level | Candidates Sampled | Runtime Stable | Runtime Unstable | Median Ratio | Notes |
|---|---:|---:|---:|---:|---|
| plan-only | | | | | |
| estimated-only | | | | | |
| profile-supported | | | | | |
| profile-and-latency | | | | | |

---

## 12. Candidate Triage and Reduction

### 12.1 实验目的

从大量 unique issue candidates 中筛选最值得人工复核和提交 issue 的代表性案例，并尽可能缩小查询和图数据。

### 12.2 为论文哪部分提供论据

支持 RQ1 和 RQ6：

```text
GraphSymPerf 不仅能产生候选，还能通过聚类、排序和 reduction 提供开发者可复现案例。
```

### 12.3 实现方法

生成 top candidates：

```bash
graph-sym-perf triage \
  --run-dir runs/main_effectiveness_2026_01 \
  --top 100
```

对 top candidates 做 replay/runtime/reduction：

```bash
graph-sym-perf validate \
  --triage runs/main_effectiveness_2026_01/triage_top100.json \
  --runtime-repeats 15 \
  --reduce
```

排序优先级：

```text
1. profile-and-latency-supported。
2. profile-supported。
3. root cause 清晰。
4. db_hits / actual_rows / latency ratio 高。
5. strict/conservative rule。
6. reducer 后 query 短。
7. graph reproducer 小。
8. 不与已有 issue 重复。
```

### 12.4 预期结果

```text
top 100 candidates 被缩减成 top 20~30 high-priority reportable issues。
每个 high-priority issue 有 base query、transformed query、graph setup、profile diff、runtime validation、root-cause label、issue draft。
```

### 12.5 产出

```text
triage_top100.json
triage_top100.md
triage_top100.csv
validation_summary.json
reduction_summary.json
issue_drafts/<case_id>.md
reportable_issues.json
```

### 12.6 人工复核标准

提交 issue 前必须人工检查：

```text
1. base 和 transformed 结果等价。
2. 现象在 clean database 上可复现。
3. profile delta 明确。
4. runtime 若作为证据，必须稳定。
5. 查询和图数据尽量最小。
6. 目标系统 issue tracker 中没有重复 issue。
7. issue 文案使用 performance anomaly candidate / missed optimization opportunity，而不是直接称 confirmed bug。
```

---

## 13. Issue Reporting and Developer Feedback Tracking

### 13.1 实验目的

将高质量候选提交给数据库开发者，获取外部验证。开发者反馈是论文有效性评估的重要证据。

### 13.2 为论文哪部分提供论据

支持：

```text
RQ1：发现真实系统问题。
RQ6：developer status taxonomy。
RQ7：可复现案例与实际修复。
```

### 13.3 实现方法

对每个 reportable issue，生成固定结构 issue draft：

```text
1. System/version/docker digest
2. Minimal graph size
3. Base query
4. Transformed query
5. Equivalence argument
6. Result equality evidence
7. Metric delta summary
8. Plan-shape diff
9. Reproduction script
10. Why this matters / suspected missed optimization
```

提交后记录：

```text
issue_url
submitted_at
developer_status
developer_comment_summary
fixed_version
root_cause_update
```

### 13.4 预期结果

可能状态包括：

```text
confirmed
fixed
wont_fix
upstream_limitation
pending
rejected_or_noise
```

不要把所有 pending candidates 都写成 confirmed bugs。

### 13.5 产出

```text
reported_issues.yaml
reported_issues.csv
developer_feedback_summary.md
issue_status_table.csv
```

### 13.6 论文表格建议

| System | Issue | Transform | Root Cause | Metric Delta | Status | Developer Feedback |
|---|---|---|---|---|---|---|
| Neo4j | #... | bool_redundancy | PredicatePushdownBlocked | DB hits 4.2x | fixed | ... |

---

## 14. Final Paper Table Export

### 14.1 实验目的

将所有实验结果转换为论文可用表格和图数据。

### 14.2 为论文哪部分提供论据

直接支持 Evaluation 章节的所有 RQ。

### 14.3 实现方法

```bash
graph-sym-perf report \
  --run-dir runs/main_effectiveness_2026_01
```

如果包含 ablation：

```bash
graph-sym-perf report \
  --run-dir runs/main_effectiveness_2026_01 \
  --ablation-runs runs/ablation_bool_static,runs/ablation_symmetry_static,runs/ablation_both_static,runs/ablation_both_feedback \
  --oracle-run runs/oracle_reliability
```

### 14.4 产出表格

建议至少导出：

```text
tables/system_setup.csv
tables/effectiveness_by_system.csv
tables/candidate_funnel.csv
tables/transform_family_contribution.csv
tables/rule_contribution.csv
tables/ablation_comparison.csv
tables/pmc_by_family.csv
tables/pmc_by_rule.csv
tables/pmc_by_kind.csv
tables/root_cause_distribution.csv
tables/oracle_reliability.csv
tables/reported_issues.csv
```

### 14.5 产出图数据

```text
figures/pmc_growth_curve.csv
figures/candidate_funnel_by_system.csv
figures/rule_reward_curve.csv
figures/duplicate_reduction_curve.csv
figures/runtime_ratio_distribution.csv
```

### 14.6 预期结果

论文可以围绕以下结果组织：

```text
1. GraphSymPerf 在多个系统上发现 profile-supported / runtime-supported candidates。
2. bool_redundancy 与 symmetry 暴露不同 root causes。
3. feedback-guided 策略在同等预算下提高 PMC 和 unique candidate density。
4. 分层 oracle 比 runtime-only 更可靠。
5. 聚类去重大幅减少人工复核工作量。
6. 若干候选被开发者确认、修复或归因于 upstream limitation。
```

---

## 15. 推荐时间日程

以下是建议的服务器实验日程。可根据服务器资源调整。

### Week 0：代码冻结与本地准备

| 时间 | 任务 | 产出 | 是否进入下一步 |
|---|---|---|---|
| Day 1 | 完成 PMC / feedback / clustering / experiment runner | 代码提交 | pytest 通过 |
| Day 2 | 本地 dry-run 和配置检查 | dry-run logs | 配置可解析 |
| Day 3 | 上传服务器，准备 DBMS 环境 | server setup log | doctor 可运行 |

### Week 1：环境校验与校准

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1 | Doctor / Preflight | preflight.json/md | 环境通过 |
| Day 2 | Known Issue Replay | replay_summary | replay 可用 |
| Day 3 | Calibration Run | calibration summary | 参数初步稳定 |
| Day 4 | 修 adapter/parser/threshold | patch + tests | pytest 通过 |
| Day 5 | 第二次 Calibration | calibration_decisions.md | 进入主实验 |

### Week 2：主实验第一轮

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1 | 启动小规模 main run，例如每系统 5k | shard summaries | 无系统性失败 |
| Day 2 | Aggregate 第一轮 | aggregate_summary | 检查 candidate funnel |
| Day 3 | 调整 timeout / thresholds / feedback | config patch | rerun small |
| Day 4-5 | 启动正式 main run shard | shard outputs | 持续监控 |

### Week 3：主实验完成与聚合

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1-3 | 完成所有 shard | shard summaries | no crash |
| Day 4 | Aggregate 全量结果 | aggregate tables | unique issues 可用 |
| Day 5 | 初步 triage top 100 | triage_top100 | 进入 validation |

### Week 4：消融实验

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1 | bool_only_static | summary | 完成 |
| Day 2 | symmetry_only_static | summary | 完成 |
| Day 3 | both_static | summary | 完成 |
| Day 4 | both_feedback | summary | 完成 |
| Day 5 | Ablation aggregate | ablation_comparison.csv | 可写 RQ2/RQ4 |

### Week 5：Oracle reliability 与 candidate validation

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1 | Runtime validation top candidates | runtime results | 稳定性统计 |
| Day 2 | Reducer top candidates | reduced cases | issue drafts 可生成 |
| Day 3 | Manual review top 30 | reportable list | 提交 issue |
| Day 4-5 | 提交第一批 issue | issue URLs | 记录状态 |

### Week 6：反馈跟踪与论文表格

| 时间 | 任务 | 产出 | 判定 |
|---|---|---|---|
| Day 1-2 | Developer feedback tracking | issue_status_table | 更新 taxonomy |
| Day 3 | Final report export | tables/ figures/ | 写 Evaluation |
| Day 4-5 | 补实验或重跑失败系统 | patched tables | 数据冻结 |

---

## 16. 主实验监控指标

服务器运行时建议每隔固定时间检查：

```text
1. completed cases
2. timeout rate
3. error rate
4. equivalence pass rate
5. profile availability rate
6. PMC new units growth
7. raw candidate rate
8. duplicate rate
9. feedback top rules
10. disk usage
```

建议阈值：

```text
timeout rate > 30%：暂停，缩小 query/graph 或增加 timeout。
equivalence pass rate < 20%：检查 transformation soundness。
profile availability < 50%：检查 adapter/parser。
PMC no growth for long window：检查 feedback/rule diversity。
disk usage > 80%：压缩或清理 raw successful cases。
```

---

## 17. 结果解释原则

论文写作和实验分析时遵守以下原则：

```text
1. raw candidate 不是 bug。
2. profile-supported candidate 不是 developer-confirmed bug。
3. estimated-only anomaly 不能直接称 runtime slowdown。
4. 不跨系统比较绝对 DB hits / rows / cost，只比较同一系统同一 query pair 内的 ratio。
5. fixed / confirmed / wont-fix / upstream / pending 必须分开统计。
6. exploratory rule 结果单独统计，不和 strict/conservative 混合。
7. runtime 是增强证据，不是唯一 oracle。
8. reducer 后若症状消失，不能用 reduced query 报告原症状。
9. 开发者反馈可能受产品优先级影响，wont-fix 不等于工具错误。
```

---

## 18. 最终应形成的论文 Evaluation 结构

建议 Evaluation 章节按以下顺序组织：

```text
6.1 Experimental Setup
    DBMS versions, server environment, workloads, transforms, thresholds.

6.2 RQ1: Effectiveness
    candidate funnel, unique issue candidates, reported/confirmed/fixed.

6.3 RQ2: Transform Family Contribution
    bool_redundancy vs symmetry, rule-level contribution.

6.4 RQ3: Plan Metamorphic Coverage
    PMC growth, PMC by rule/family, correlation with candidates.

6.5 RQ4: Feedback Guidance
    static vs feedback, same budget comparison.

6.6 RQ5: Oracle Reliability
    plan-only/profile/runtime validation, noise discussion.

6.7 RQ6: Root-Cause Taxonomy and Deduplication
    root-cause distribution, duplicate reduction, representative cases.

6.8 Case Studies
    3~5 high-quality cases with plan/profile explanation.

6.9 Threats to Validity
    runtime noise, metric inconsistency, Cypher subset, developer feedback latency.
```

---

## 19. Checklist：服务器正式启动前

```text
[ ] python -m pytest 通过。
[ ] config/experiments/*.yaml 可解析。
[ ] doctor 通过。
[ ] replay_known 通过或失败原因可解释。
[ ] calibration 至少完成一次。
[ ] thresholds 已根据 calibration 调整。
[ ] output_root 磁盘空间充足。
[ ] 每个 DBMS 版本和 Docker digest 已记录。
[ ] shard/resume 已在小规模测试中验证。
[ ] aggregate 能合并 fake 或 small shard。
[ ] triage 能输出 top representatives。
[ ] runtime validation 能对一个 case 跑通。
[ ] issue draft 模板可生成。
```

---

## 20. Checklist：主实验完成后

```text
[ ] 所有 shard 完成或失败原因记录。
[ ] aggregate_summary 生成。
[ ] aggregate_pmc_summary 生成。
[ ] aggregate_clustered_summary 生成。
[ ] unique_issues.json 生成。
[ ] triage_top100 生成。
[ ] top candidates validation 完成。
[ ] issue drafts 生成。
[ ] reported_issues.yaml 更新。
[ ] tables/*.csv 导出。
[ ] figures/*.csv 导出。
[ ] 所有关键表格能对应到论文 RQ。
```

---

## 21. 一句话执行策略

先用 known issue replay 证明环境和工具是可信的，再用 calibration 确定阈值和资源配置，然后用 main effectiveness 大规模寻找 unknown candidates，用 ablation 证明两个等价变换族和反馈机制的贡献，用 oracle reliability 证明分层 oracle 的必要性，最后通过 triage、reducer 和 developer feedback 把高质量候选转化为论文中的真实案例和可复现证据。

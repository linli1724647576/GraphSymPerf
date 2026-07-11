# 主实验运行手册

## 运行前

确认 7 个 GDBMS 版本和隔离 slot；GDsmith checkout 位于配置指定路径且 commit/dirty 状态满足 doctor；6 个容器型目标提供连接变量、`GRAPHSYMPERF_<DBMS>_CONTAINER=<name-or-id>` 及 `GRAPHSYMPERF_<DBMS>_IMAGE_DIGEST=sha256:<64 hex>`，两者须经 Docker 实测匹配；LadybugDB 使用 `GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST`绑定实际安装的嵌入式包；磁盘至少覆盖 112 × 500 MiB 上限。

从 `.env.example` 复制本地 `.env`（不提交）。先从固定且干净的 GDsmith checkout 构建内容寻址 JAR，再生成不含密码的实际执行产物 pin：

```bash
python scripts/build_agensgraph_gdsmith_compat.py \
  --src ../GDsmith-src-runtime-first \
  --out ../GDsmith-artifacts/GDsmith-runtime-first.jar
python scripts/render_local_provenance.py \
  --jar ../GDsmith-artifacts/GDsmith-runtime-first.jar
```

将第二个命令的输出填入 `.env`。正式 CLI 自动加载根目录 `.env`，resolved manifest 只记录变量是否存在和产物摘要，不序列化 URI 凭据或密码。

advanced 预检：

```bash
graphsymperf experiment doctor --config config/experiment.yaml
graphsymperf experiment plan --config config/experiment.yaml
```

plan 必须输出 systems=7、shards=112、accepted_pair_budget=112000，且 manifest 有 112 个唯一 seed。

## 递进实跑门禁

对每个目标按顺序执行；100 对必须已有同目标的完整 10 对运行，1,000 对必须已有完整 100 对运行：

```bash
for target in neo4j memgraph falkordb agensgraph apache_age arcadedb ladybugdb; do
  graphsymperf experiment gate --target "$target" --accepted-pairs 10
  graphsymperf experiment gate --target "$target" --accepted-pairs 100
  graphsymperf experiment gate --target "$target" --accepted-pairs 1000
done
```

门禁使用与正式实验同一 worker、validation、reducer、artifact 契约和实际 DBMS 来源校验，仅将矩阵收缩为单目标单 shard。任一级出现 `INCOMPLETE`/`FAILED`、证据链无效或超出 artifact 预算，都不得启动下一级。

## 唯一正式命令

```bash
graphsymperf experiment run --config config/experiment.yaml
```

不在重构或清理提交中自动启动长跑。该命令创建全新 run ID，不读取旧 `runs/`、`logs/` 或旧结果。

## 恢复与状态

```bash
graphsymperf experiment resume --run-id <run-id> --config config/experiment.yaml
graphsymperf experiment status --run-id <run-id>
```

resume 必须使用原配置解析出完全相同的 immutable manifest。`INCOMPLETE` 表示 accepted count 或阶段链不足，应检查 `checkpoint.json`、`failure.json`、`events.jsonl`、`artifact_warnings.jsonl` 和最后 attempt；不能手工修改计数后恢复。

## 结果核对

每 shard 检查 `accepted_pairs.jsonl`、`candidates.jsonl`、`attempt-*/validation.json`、`attempt-*/reduction.json` 和 `candidate-outcomes/`。validation/reducer 的 content-addressed 对象在 shard `artifacts/`；SubmitReadyIssue 索引与 typed export 在 run 级 `issues/index/`、`issues/submit-ready/`。ReliabilityFinding 单独位于 run 级可靠性记录，不计性能 issue。

# Gremlin 新增目标选择（2026-10-18）

核查日期：**2026-10-18，北京时间**。下文发布、提报、评论和合入时间统一使用 **UTC**；北京时间为 UTC + 8 小时。GitHub 关注数是当日 API 快照，不代表市场份额或学术影响力。

## 选择结论与范围

本轮选择 **ArcadeDB 26.10.2 的 Gremlin 语言目标**，新增一个可本地部署的工业数据库目标，满足“新增 1–2 个”的范围。仓库已经有 ArcadeDB 的 Cypher 适配器；本轮增加的是同一数据库的 Gremlin 接入、接口验证和现有 Pattern 实验，**不计为新增一个独立 DBMS，也不增加 Pattern 或 Native 探针**。

选择理由是近期有可核查的修复、公开源码和固定发行版可复现，以及官方提供 Gremlin 接口。现有证据**不足以断言 ArcadeDB 的总体影响力超过 HugeGraph**；本轮优先考虑修复活跃程度与实验可复现性。

Apache TinkerGraph 留作未接入备选。它是 TinkerPop 的内存参考图引擎，适合定位公共 Gremlin 优化策略，但本轮不将它作为第二个独立工业数据库。OrientDB 和 Amazon Neptune 暂不接入，具体依据见候选表。

## 候选与证据

| 候选 | 当日可核查事实 | 与本轮要求的关系 | 决定 |
|---|---|---|---|
| ArcadeDB | GitHub 1,205 stars；最新 release `26.10.2`，2026-10-15 18:03:41 UTC 发布；固定 tag 的 `pom.xml` 声明 TinkerPop `3.8.2` | 可本地部署；HTTP/JSON 支持 `language: gremlin`，也有 Gremlin Server 插件；有下表具体处理记录 | 本轮接入其 Gremlin 语言目标 |
| Apache TinkerGraph / TinkerPop | TinkerPop GitHub 2,147 stars；稳定版 `3.8.2` 于 2026-09-01 发布；官方提供 Gremlin Server 二进制包及 Docker 镜像 | 单机内存图引擎，支持可选持久化；参考实现身份有助于公共优化器定位，但不能据此充当第二个独立工业存储系统 | 未接入备选 |
| OrientDB | 核心仓库 4,993 stars；`3.2.57` 于 2026-10-05 10:44:23 UTC 发布；另有 `orientdb-gremlin` 仓库和 TP3 发行 | 核心维护仍活跃，但不能推导 Gremlin 报告处理快；Gremlin #180 从 2021-01-19 起仍开放且无评论，#141 从 2018-02-03 起仍开放 | 暂不接入；不称其停更 |
| Amazon Neptune | 官方支持 Gremlin HTTPS 接口，但明确不支持请求的 `bindings` 属性；连接文档要求 Neptune 实例及对应网络环境 | 当前接口以独立绑定传参为主，且本轮没有可运行的 Neptune 实例或凭据；不能保持现有接口直接本地执行 | 暂不接入 |
| Apache HugeGraph（已有目标） | GitHub 3,192 stars；最新 GitHub release 为 `1.7.0`，2025-11-16 15:28:32 UTC 发布 | 关注数高于 ArcadeDB 和 TinkerPop；这些数字不能支持“新增目标必然更有影响力”的结论 | 保留已有目标 |

来源：[ArcadeDB 仓库 API](https://api.github.com/repos/ArcadeData/arcadedb)、[ArcadeDB 26.10.2 release](https://github.com/ArcadeData/arcadedb/releases/tag/26.10.2)、[固定 tag 的依赖版本](https://github.com/ArcadeData/arcadedb/blob/26.10.2/pom.xml#L106-L115)、[ArcadeDB Gremlin 官方接口](https://docs.arcadedb.com/arcadedb/reference/gremlin/gremlin)、[TinkerPop 仓库 API](https://api.github.com/repos/apache/tinkerpop)、[TinkerPop 官方下载](https://tinkerpop.apache.org/download.html)、[TinkerGraph 3.8.2 文档源码](https://github.com/apache/tinkerpop/blob/3.8.2/docs/src/reference/implementations-tinkergraph.asciidoc)、[OrientDB 仓库 API](https://api.github.com/repos/orientechnologies/orientdb)、[OrientDB 3.2.57 release](https://github.com/orientechnologies/orientdb/releases/tag/3.2.57)、[OrientDB TP3 官方接入](https://orientdb.dev/docs/3.1.x/tinkerpop3/OrientDB-TinkerPop3.html)、[OrientDB Gremlin #180](https://github.com/orientechnologies/orientdb-gremlin/issues/180)、[OrientDB Gremlin #141](https://github.com/orientechnologies/orientdb-gremlin/issues/141)、[Neptune Gremlin API](https://docs.aws.amazon.com/neptune/latest/userguide/gremlin-api-reference.html)、[Neptune 连接前提](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-gremlin.html)、[HugeGraph 仓库 API](https://api.github.com/repos/apache/hugegraph)、[HugeGraph 1.7.0 release](https://github.com/apache/hugegraph/releases/tag/1.7.0)。

## ArcadeDB 的四个处理样本

以下样本来自官方 issue、PR 与 GitHub API 的时间字段，按相关性选择，**不是随机样本、平均响应时间或服务等级承诺（SLA）**。“首条维护者评论”不等于维护者实际开始处理的时间；“合入”也不自动等于已随发行版发布。

| 样本 | 提报时间（UTC） | 首条维护者评论（UTC） | 修复合入时间（UTC）及提报至合入耗时 | 核查后的状态与边界 |
|---|---|---|---|---|
| [#9580：COALESCE 非空常量后仍执行 COUNT](https://github.com/ArcadeData/arcadedb/issues/9580) → [#9583](https://github.com/ArcadeData/arcadedb/pull/9583) | 2026-10-09 16:14:41 | 2026-10-09 19:58:05，`lvca` | 2026-10-09 19:56:45；约 3 小时 42 分钟 | 这是 Cypher 表达式执行的修复样本，不能当作 Gremlin 优化器已修复的证据。修复提交已包含在 `26.10.2` tag 中 |
| [#9781：Gremlin 嵌套 map 的元素标识被同名属性覆盖](https://github.com/ArcadeData/arcadedb/issues/9781) → [#9897](https://github.com/ArcadeData/arcadedb/pull/9897) | 2026-10-11 22:01:49 | 2026-10-13 12:00:41，`lvca` | 2026-10-13 11:59:55；约 37 小时 58 分钟 | Gremlin 结果转换修复；修复提交已包含在 `26.10.2` tag 中 |
| [#10105：Gremlin 请求的客户端归属缺失](https://github.com/ArcadeData/arcadedb/issues/10105) → [#10112](https://github.com/ArcadeData/arcadedb/pull/10112) | 2026-10-15 14:38:05 | 2026-10-15 20:19:05，`lvca` | 2026-10-15 20:18:05；5 小时 40 分钟 | 维护者明确只确认一部分：session 情形不成立，因为 ArcadeDB 拒绝 Gremlin session；非默认 channelizer 的客户端信息问题已修复。合入晚于 `26.10.2` 发布，当前 milestone 为 `26.11.1` |
| [#10196：已删除 Gremlin 元素仍可参与写入](https://github.com/ArcadeData/arcadedb/issues/10196) → [#10250](https://github.com/ArcadeData/arcadedb/pull/10250) | 2026-10-17 10:28:01 | 2026-10-18 02:59:44，`lvca` | 2026-10-18 02:59:07；约 16 小时 31 分钟 | 维护者确认修复，报告者随后在 `main a2c54ef20f` 复核；发生在 `26.10.2` 发布之后，当前 issue milestone 为 `26.11.1`。它是元素生命周期/正确性问题，不是本轮现有只读查询对的性能结果 |

版本判定特别注意：#9580、#9781 的维护者当时评论写了 `26.11.1`，但不能沿用评论直接断言修复未发布。GitHub compare API 证实 #9583 的合入提交 `9d0bc88434f45563a9189999b255fcb0a173d59a`、#9897 的合入提交 `09aa4775a3162a5cc86ea967df248de4e938f257` 均为 `26.10.2` 的祖先；固定 tag 的 `ArcadeGremlin.java` 也包含 #9781 的递归 map 规范化实现。相反，#10112 的合入提交在 `26.10.2` 之后。

可重查：[比较 #9583 与 26.10.2](https://api.github.com/repos/ArcadeData/arcadedb/compare/9d0bc88434f45563a9189999b255fcb0a173d59a...26.10.2)、[比较 #9897 与 26.10.2](https://api.github.com/repos/ArcadeData/arcadedb/compare/09aa4775a3162a5cc86ea967df248de4e938f257...26.10.2)、[比较 #10112 与 26.10.2](https://api.github.com/repos/ArcadeData/arcadedb/compare/eaf0fe653063c09c990eeb188cbd82c52e070528...26.10.2)、[26.10.2 的结果转换源码](https://github.com/ArcadeData/arcadedb/blob/26.10.2/gremlin/src/main/java/com/arcadedb/gremlin/ArcadeGremlin.java)。

## 接入和报告的证据边界

1. 只复用当前查询中间表示、Gremlin 编译器、Pattern、受控图状态和测量流程。不能因数据库增加而引入新 Pattern 或把 Native 机制并入 Pattern 贡献。
2. 固定 `26.10.2` 发行版后，先检查协议、绑定参数、schema/索引、装载与事务、完整结果、类型、NULL、错误/超时、PROFILE 和重放；本文件只完成目标选择，不证明这些检查已经通过。
3. 查询对必须先通过完整结果等价性，再进入普通运行时间比较；PROFILE 单独采集并用于解释工作量。
4. 沿用 [现有 Gremlin 证据边界](gremlin-support.md)：显式遍历（`explicit_traversal`）超过阈值只构成 `timing_observation`。还需证明该版本适用的优化规则遗漏了可避免工作，排除起点、方向或顺序本来规定的工作量差异，才登记遗漏优化（missed optimization）。
5. 只有通过当前阈值与独立确认、具备可重放材料并完成根因去重的真实问题，才按 [BUG list/AGENTS.md](../BUG%20list/AGENTS.md) 写入 `BUG list`。维护活跃或已有修复样本不能替代新问题的本地证据；本文也不代表已向上游提交 issue。

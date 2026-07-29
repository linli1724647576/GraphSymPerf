# 正文图表规划

> 当前尚未确定具体会议模板，物理栏宽与字体规格标记为 `custom/TBD`。第一版只在主文档中嵌入 F1 的 Mermaid 草图，正式投稿图待实验结果冻结后制作。

## F1 方法与证据链概览

| 字段 | 内容 |
|---|---|
| 绑定 claim | GraphSymPerf 将安全变换、独立运行时验证、成对归约和问题生成连成不可跳级的证据链 |
| 故事 | 从 GDsmith 基础查询到 SubmitReadyIssue；plan/PROFILE 只用于解释 |
| 放置 | Introduction 末或 Method 开头 |
| 优先级 | 必做 |
| 图型 | 横向流程图，区分决定真值的实线和解释证据的虚线 |
| 数据需求 | 无数值；取自方法状态机 |
| 无障碍 | 不只依赖颜色，用线型和节点标签编码 |
| 规格 | 双栏通栏；`custom/TBD`；最终优先 SVG/PDF |

## F2 候选存活漏斗

| 字段 | 内容 |
|---|---|
| 绑定 claim | 独立验证和归约后验证过滤了发现阶段的非稳定候选 |
| 故事 | accepted → screen → validation → reduced valid → submit ready → submitted 的数量变化及拒绝原因 |
| 放置 | Evaluation / RQ2 |
| 优先级 | 必做，但等待正式矩阵 |
| 图型 | 分 DBMS 的 small multiples 漏斗或 alluvial；旁列主要拒绝原因 |
| 数据需求 | 正式矩阵各阶段状态与 reason code |
| 误导防线 | 每层同时给数量和相对上一层比例；公开累计 issue 不混入漏斗 |
| 规格 | 双栏通栏；Okabe–Ito + 纹理/标记；`custom/TBD` |

## T1 跨数据库主结果

| 字段 | 内容 |
|---|---|
| 绑定 claim | 同一协议在 7 个图数据库上的有效性和通用性 |
| 放置 | Evaluation / RQ1、RQ5 |
| 优先级 | 必做 |
| 行 | 7 个 DBMS + Total |
| 列 | accepted、screen、validation passed、reduced valid、submit ready、submitted 及主要拒绝率 |
| 数值要求 | 计数和分母并列；不使用仅有百分比的单元格 |
| 来源 | 正式 run manifests 与 typed artifacts |
| 规格 | 三线表；双栏通栏；单位和状态定义写入表注 |

## T2 规则效用与归约摘要

| 字段 | 内容 |
|---|---|
| 绑定 claim | 不同 BOOL/SYM 核心规则在适用率、验证产出和归约效果上存在差异 |
| 放置 | Evaluation / RQ3、RQ4 |
| 优先级 | 必做 |
| 行 | 核心 rule × DBMS，必要时拆主表和附录 |
| 列 | applicable、accepted、quarantined、validated、字符减少、AST 减少、1-minimal |
| 来源 | rule registry、validation 和 reduction artifacts |
| 规格 | 正文仅放聚合表，完整矩阵入附录；三线表，无竖线 |

## 可删项

29 条累计 issue 的状态分布可保留为一张简表，不建议再画饼图或装饰性柱图。它不是正式矩阵结果，图形强化容易让读者误把累计问题集当成当前协议产出。


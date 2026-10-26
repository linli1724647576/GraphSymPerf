# Random AST Generation 红框区域设计

## 推荐的三层结构

这一区域采用“子句采样 → 类型驱动展开 → 抽象 AST”的纵向过程。选择分支用来表达随机性，最终 AST 用来表达生成产物。

1. **Sample clauses：采样子句骨架。** 以 MATCH、WITH、RETURN 为示例候选，本次选择 MATCH 和 RETURN，形成 `MATCH → RETURN`。候选是子句产生式，不是保存好的完整查询。
2. **Typed expansion：按类型选择表达式结构。** 返回位置需要整数，因此展示 CASE、COUNT、Literal 等合法形式。本次选择 COUNT，其他形式保留为灰色虚线。类型、变量作用域和预算共同筛选允许的选择。
3. **Abstract AST A₀：输出带接口的抽象 AST。** 根节点 Query 分别连接 MATCH 与 RETURN。MATCH 中标签 α 尚未具体绑定；RETURN 通过 COUNT 消费一个待填充的结果表接口。

图中仅展示少量候选和一次展开示例。当前实际生成语法还包含其他子句形式和表达式结构。

## 视觉含义

| 表示 | 含义 |
|---|---|
| 黑色实线和实框 | 本次选择与生成的结构 |
| 灰色虚线和灰字 | 其他合法但本次未选择的结构 |
| `Expr : int` | 当前表达式位置需要整数值 |
| `(n : α)` | 节点变量 n 与尚未绑定的标签终端 α |
| 底部空方框 | 唯一的结果表型 Pattern 插入口，由后续嵌入阶段填充 |

COUNT 在这里只确定“统计后续子查询的结果行数”这一结构；具体子查询由 M2 嵌入。图中因此不放入 P3、S7 或具体图数据。

## 在原图中的使用

- [局部可编辑 SVG](Random_AST_Generation_panel.svg)：330 × 720，比例对应用户图中的窄长红框，文字和节点均为原生矢量元素。
- [局部放大 PNG](Random_AST_Generation_panel.png)：660 × 1440，便于查看细节。
- [放回原图的 PNG 预览](Overview_AST_panel_preview.png)：只替换用户标红的区域，便于比较布局。
- [放回原图的 SVG 预览](Overview_AST_panel_preview.svg)：以用户原截图为背景，新增 M1 区域为矢量；背景截图不属于矢量重建。

从最终 A₀ 向右侧 Pattern Embedding 连接输出箭头即可表达后续衔接。该预览只展示红框区域的设计，不调整原图其他阶段。

重建局部 SVG：

```bash
.venv/bin/python picture/generate_random_ast_panel.py
```

使用同一张用户截图重建放置预览：

```bash
.venv/bin/python picture/generate_random_ast_panel.py --overview <1950x858的原始PNG路径>
```

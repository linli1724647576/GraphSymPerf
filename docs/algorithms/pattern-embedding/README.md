# M2 Pattern 嵌入：可读版伪代码

参照 SCor Algorithm 1 和 GDsmith Algorithms 2–3 的表达方式，使用易懂的对象名和简短动作，不在主算法中展开位置地址、语义边界和变异记录的内部字段。算法行为仍对应当前 M2。

```text
Algorithm 2  将 Pattern 嵌入 AST
Input:  抽象 AST ast，待嵌入的 Pattern pattern
Output: 嵌入后的 AST tree，或拒绝

1:  slot ← 获取 ast 中预留的插槽
2:  if slot 与 pattern 的接口类型不兼容 then
3:      return Reject
4:  end if
5:  fragment ← 将 pattern 展开为局部 AST
6:  重命名 fragment 中的私有变量，避免与外层重名
7:  tree ← 用 fragment 替换 ast 中的 slot
8:  标记插入结构中后续需要变异的节点
9:  记录变异方式及其完整保护上下文
10: 保存后续构图需要满足的 Pattern 条件
11: if tree 的类型、作用域或插入范围检查失败 then
12:     return Reject
13: end if
14: 固定 tree 的生成结构，仅允许已记录的变异
15: return tree
```

## 与方法对应的含义

- `ExpandPattern` 固定局部结构并进行已声明的接口适配，不构图、不执行变异，也不选择具体图数据。
- 重命名覆盖局部源结构、变异备选和条件中的私有变量；显式导入的外层绑定保持原义。
- 第8–9行一起保存“改哪里、怎么改、哪些上下文需要保留”，供 M5 变异和 M6 传播检查使用。
- 第10行保存的条件由 M3 构造图状态时满足和检查。
- 第11行还检查插槽以外的结构保持不变，以及局部展开未超过预留预算。
- 第14行允许 M3／M4填充符号终端；结构变化仅限 M5 执行已登记的局部变异。

输入 Pattern 已由本轮实验指定。不兼容时返回拒绝，由外层重试；如需重新选择 Pattern，应在外层选择后再次调用本算法。

## 参考写法与文件

- [SCor 正式论文](../../../reference/Scor.pdf)：Algorithm 1，印刷页184:10。
- [GDsmith 正式论文](https://taoxiease.github.io/publications/issta23-gdsmith.pdf)：Algorithms 2–3。
- [可编辑 SVG](../../../picture/Algorithm_2_pattern_embedding.svg)
- [PNG 预览](../../../picture/Algorithm_2_pattern_embedding.png)
- [LaTeX 片段](algorithm.tex)／[独立主文件](main.tex)

SVG 和 LaTeX 共用 [同一生成源](../../../picture/algorithm_assets/build_pattern_embedding_algorithm.py)，均为15行。这里借鉴两篇论文的算法写作方式，所描述的嵌入流程为 GraphSymPerf 自身的方法。

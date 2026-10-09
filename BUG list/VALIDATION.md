# 验证记录

本批32个案例均在2026-10-09重新构图、检查完整类型化结果、采集实际图规模和PROFILE。10个可报告案例均通过两次重建状态的独立会话确认；P5-02保留未通过3倍门槛的原始数据，P16排除缺陷认定。

全部32个独立脚本经过语法、附件、相对链接及输入校验和核对；原生索引语法按对应数据库转换。Neo4j原始ASCII与FalkorDB原始计划行保持一致，Memgraph表由保存的PROFILE行忠实渲染。

以下三例额外使用报告目录内的reproduce.py实际执行setup/counts/queries，脚本不依赖Src：

- [Neo4j P3 CASE02](可报告/Neo4j/P3/CASE02/ISSUE.md)：20,004节点、20,003关系，两条查询均返回同一个两跳见证。[运行输出](validation/neo4j-portable-reproducer.txt)
- [Memgraph P13 CASE01](可报告/Memgraph/P13/CASE01/ISSUE.md)：200,014节点、200,011关系，十行结果以及两行NULL延展保持一致。[运行输出](validation/memgraph-portable-reproducer.txt)
- [FalkorDB P10 CASE02](可报告/FalkorDB/P10/CASE02/ISSUE.md)：250,003节点、200,001关系，两条查询的完整结果均为空；高侧成功见证仍保留。[运行输出](validation/falkordb-portable-reproducer.txt)

本次验证不将原发现轮的40条候选改计为新的独立BUG。报告发布状态与开发者确认仍为空。

# GraphSymPerf documentation / 文档

The [project README](../README.md) describes installation and execution. This directory documents the testing method, implementation, database configuration and report maintenance.

安装和运行方法见[中文 README](../README.zh-CN.md)。本目录提供测试方法、实现架构、数据库配置和缺陷报告维护说明。

| Topic / 主题 | English | 中文 |
| --- | --- | --- |
| Method / 测试方法 | [Method](approach.md) | [方法说明](approach.zh-CN.md) |
| Pattern reference / 测试模式 | [Pattern catalog](pattern-catalog.en.md) | [模式目录](pattern-catalog.zh-CN.md) |
| Implementation / 实现架构 | [Code architecture](architecture.md) | [代码架构](architecture.zh-CN.md) |
| Database setup / 数据库部署 | [Backend setup](backends.md) | — |
| Testing / 测试与验收 | [Testing and validation](testing.md) | — |
| Bug reports / 缺陷报告 | [Report guide and index](bug-reports.md) | — |

`bugs.csv` is the source for issue statuses, evidence links and report paths. Run `python scripts/update_readme.py` after editing the registry to update the root README tables and the report index. Maintain method, implementation and setup instructions in their respective documents.

缺陷状态、证据链接和报告路径统一维护在 `bugs.csv`。修改台账后运行 `python scripts/update_readme.py`，更新根目录 README 表格和报告索引。方法、实现和部署说明在对应文档中维护。

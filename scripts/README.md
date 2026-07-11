# Scripts

正式实验不通过脚本启动。唯一入口是：

```bash
graphsymperf experiment run --config config/experiment.yaml
```

本目录只允许放审计、迁移和维护工具；工具必须默认只读或 dry-run，不能形成第二套 runner、配置默认值或 issue promotion 路径。一次性 mining、旧 replay、cluster、PMC/PROFILE 汇总与多配置调度脚本已移除。

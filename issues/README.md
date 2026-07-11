# Issue evidence packages

`issues/evidence/` 只保存 11 个已公开 issue 的最小证据包。每个目录必须包含：

- `metadata.yaml`
- `setup.cypher`
- `original.cypher`
- `transformed.cypher`
- `reduced_original.cypher`
- `reduced_transformed.cypher`
- `validation.json`
- `README.md`
- `evidence_inventory.json`（上述八个文件的 SHA-256 清单）

这些目录是历史公开证据，不是新版 runtime-first v3 验证结果。`revalidated_by_v3: false` 表示该 issue 尚未完成两个独立 validation session、负对照、环境/DBMS fingerprint 绑定和 reducer 后复验；缺失字段必须写明 `unknown` 原因，不得根据旧实验产物推测或补造。

`scripts/migrate_public_issue_evidence.py` 只从公开 GitHub issue 正文和公开附件重建材料，不读取或恢复用户已删除的旧 `runs/`、`logs/`、BUG 目录或实验原始产物。全局 `issues/evidence_inventory.json` 绑定 11 个证据包的 metadata 与 package inventory。

只有正式流水线产生的 `SubmitReadyIssue` 才能导出新的开发者报告；历史 evidence package 不能绕过 validator、reducer 或 promotion gate。

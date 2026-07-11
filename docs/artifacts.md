# Artifact 与证据链

## 四级保留

| Level | 内容 |
| --- | --- |
| minimal | query/pair hash、rule/site、等价性摘要、screen runtime 与错误分类 |
| candidate | query pair、parameters、discovery paired timing、SafetyCertificate、graph snapshot ref 与全部 validation-input hashes |
| validation | 两个 session 的 raw paired timing、runtime verdict/reason、A/A 与 B/B、lifecycle、环境、fingerprint、typed equivalence 与 stage；通过与失败终态使用同一 typed schema |
| issue | reduced pair、reproducer、original/final validation refs、reduction ref，以及可选 ExplanationEvidence ref |

对象按 SHA-256 内容寻址，JSONL 只保存小记录和 ref。candidate hash 绑定 query、parameter、graph、DBMS fingerprint/version、resolved config、slow direction、discovery ratio、policy 与 certificate。`verify_issue_hash_chain` 从 issue 递归解析 original/reduced candidate 与两次 validation，验证 reduced query hashes、reduction 与 reproducer。issue record 还封存 `REDUCED_VALID`、CORE certificate、duplicate clearance、validated ratio 与 DBMS version。

## 预算与恢复

每 shard 500 MiB。store 每次打开都扫描对象树并修复用量计数；写入使用临时文件、fsync 与原子 link/replace。超预算时只允许删除声明为 nonessential 的 `attempt-*/gdsmith_raw`，不得删 candidate、validation、issue 或 checkpoint；降级写入 warning。

## 公开包

历史 11 个 issue 位于 `issues/<id>/`，包含 metadata、setup、原始/变换/reduced query、validation 与 README。它们保留历史事实；`revalidated_by_v3: false` 不能被本地旧数据自动改成 true。新 SubmitReadyIssue 索引位于 `runs/<run-id>/issues/index/`。物化文件完成并 fsync 后，运行级 registry 写 `SUBMIT_READY`，最后生成绑定两者的 commit marker；任一中间崩溃都会在 exact retry 中重建。`issue export` 在共享 registry lock 下同时复核 commit marker、registry、issue hash chain 和 typed exporter 输出字段。

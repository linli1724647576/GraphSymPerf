# Issue 生命周期

## Pipeline Stage

`SCREENED` runtime candidate 先进入原始 pair validation。只有 `VALIDATION_PASSED` 才运行 reducer；只有 `REDUCED_VALID`、完整 CORE SafetyCertificate、方向保持且运行内无 exact/possible duplicate，`promote()` 才能构造 `SubmitReadyIssue`。raw dict、candidate、profile evidence 或单个 validation report 均不能调用 exporter。

possible duplicate 写入待人工去重队列；没有可核查的 distinct evidence 不自动提交。若后到案例使先前的 unique clearance 失效，运行级 revocation 会阻止旧索引继续 export。正式提交并记录上游 URL 后，stage 才从 SubmitReadyIssue 进入 `SUBMITTED`，并计为 Reported Issue Report。

## Developer Status

开发者状态与 pipeline stage 正交：waiting reply、acknowledged、confirmed、fixed、duplicate、intended behavior、uninterested、needs information。Developer-Validated Bug 只包含 confirmed 和 fixed，且必须保存维护者评论、commit、release note 或关闭理由 URL。

## 90 天观察窗

以 submitted_at 为起点固定 90 天。窗口内 waiting reply 是右删失（right-censored），不进入正例或负例；超过窗口仍单列 unresolved。论文同时报告 submitted、developer-validated、responded-negative、right-censored-waiting 和 unresolved-beyond-window，不用“回复率”替代 precision。

历史 11 个公开报告按各自 detector/version 与证据状态列示；未由 v3 双 session + reducer 复核的案例只能作为 cumulative corpus，不能标成新流水线的 validation passed。

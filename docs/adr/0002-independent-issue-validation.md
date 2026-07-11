# ADR 0002：独立 Issue Validation

- 状态：Accepted
- 日期：2026-07-10

## 背景

从海量候选选择极值会放大噪声；复用 discovery 连接、图状态或 timing 不能排除 winner's curse、缓存顺序与 stale state。

## 决策

原始 pair 与 reduced pair 各运行两个独立 OS process/session。每个 session 重建连接与图，执行完整 typed equivalence、A/A/B/B 负对照、5 warmups 和 20 balanced pairs。环境 provenance 与 graph/DBMS fingerprint 是硬门禁。

## 后果

验证成本显著增加，但 candidate 与 reportable issue 被类型和证据链隔离。session crash、timeout 或环境字段缺失产生 rejected/inconclusive/invalid，不允许降级为 pass。

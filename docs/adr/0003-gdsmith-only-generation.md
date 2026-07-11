# ADR 0003：GDsmith-only Generation

- 状态：Accepted
- 日期：2026-07-10

## 背景

多生成器会引入不可比较的 schema、query distribution、seed 和 accepted-count 口径，并增加恢复分支。

## 决策

GDsmith 是唯一 generator，固定 checkout 与 commit。AGE/LadybugDB 不伪装成 GDsmith 原生 backend，而使用隔离 proxy 完成生成，再经显式 dialect decision 在目标重放。unsupported construct 通过 REJECT/top-up 处理。

## 后果

正式实验只有一个 workload 来源和一个 seed 体系。若 GDsmith 或 proxy 未通过 doctor，整个矩阵停止；不存在替代 workload 继续跑的路径。

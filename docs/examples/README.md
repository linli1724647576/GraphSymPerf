# 六阶段方法稿的复现材料

方法正文：[GraphSymPerf-六阶段方法汇报稿](../GraphSymPerf-六阶段方法汇报稿.md)。

## 案例语义核查

在仓库根目录运行：

```bash
.venv/bin/python docs/examples/verify_six_stage_examples.py
```

脚本直接从正文抽取五段完整 Cypher，使用现有解析器、类型检查和有界解释器核查 P3、S7 及外层计数对照。保存的结果见 [six-stage-example-validation.json](six-stage-example-validation.json)。这些结果验证小图中的语义计算和逻辑需求，不是数据库性能测量。

## 重建 SVG

```bash
.venv/bin/python docs/figures/generate_method_figures.py
```

生成器只使用 Python 标准库，重建两个可编辑 SVG；PNG 为视觉预览副本。

## 重建离线 HTML 和 PDF

在能够导入 `marked` 与 `playwright` 的 Node.js 环境中运行：

```bash
node docs/render_method_report.mjs
```

可用 `GSYMPERF_NODE_MODULES` 指定现有运行时的 `node_modules`，用 `GSYMPERF_CHROME` 指定已安装的 Chrome。脚本内联两张 SVG，不依赖远程图片、字体或脚本，生成同名离线 HTML 与 PDF。

本次 PDF 已逐页检查分页、图注、算法块与中文显示；文献名称及术语对应在正文附录中统一列出。

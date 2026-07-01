# GraphSymPerf BUG list

This directory separates issue-grade performance bugs from replayed candidates that did not survive scale confirmation.

| Entry | Case | Status | Rule | Replay result |
|---|---|---|---|---|
| [BUG001](BUG001.md) | `gdsmith_case_0955` | Confirmed | Pattern Order Symmetry | Equivalent results, `206.34x` slower latency, `293.04x` more DB Hits |
| [NOT_BUG_gdsmith_case_0170](NOT_BUG_gdsmith_case_0170.md) | `gdsmith_case_0170` | Not confirmed | Label Order Symmetry | Equivalent results, same plan hash, same DB Hits and Rows |

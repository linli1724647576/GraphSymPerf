# Mutation Pattern Catalog

**Table 1: Mutation patterns and their applicability conditions. SC and SY denote short-circuit and symmetry patterns.**

`D_T` and `D_F` denote unchanged guards that evaluate to true and false, respectively, for every eligible input binding. `E_L`/`E_H` and `Q_L`/`Q_H` denote expressions and queries with low/high potential work. `Q_0` is an unchanged witness query, and `c` is a fixed constant row. `↦` denotes protected replacement; `↔` denotes symmetry. All forms are read-only, preserve the output interface, and are compared on the same graph using bag semantics.

| ID | Type | Transformation | Applicability condition |
| --- | --- | --- | --- |
| SC1 | SC | `D_T OR E_L ↦ D_T OR E_H` | The true guard is unchanged for every input binding; replacements return Boolean-or-null values. |
| SC2 | SC | `D_F AND E_L ↦ D_F AND E_H` | The false guard is unchanged for every input binding; replacements return Boolean-or-null values. |
| SC3 | SC | `CASE WHEN D_T THEN V ELSE E_L END` ↦ `CASE WHEN D_T THEN V ELSE E_H END`<br>or<br>`CASE WHEN D_T THEN V WHEN E_L THEN V ELSE V END` ↦ `CASE WHEN D_T THEN V WHEN E_H THEN V ELSE V END` | The first true condition and its selected result remain unchanged. Result replacements have compatible types; later-condition replacements return Boolean-or-null values. |
| SC4 | SC | `coalesce(NULL, V, E_L) ↦ coalesce(NULL, V, E_H)` | `V` is unchanged and non-null for every input binding, including when its value is false or zero; argument types are compatible. |
| SC5 | SC | `MATCH L RETURN c LIMIT 0` ↦ `MATCH H RETURN c LIMIT 0` | Both forms retain `LIMIT 0`; replacing the read-only match preserves the empty output. |
| SC6 | SC | `(Q_0 UNION ALL Q_L) LIMIT k` ↦ `(Q_0 UNION ALL Q_H) LIMIT k`<br>or<br>`MATCH L RETURN c LIMIT k` ↦ `MATCH H RETURN c LIMIT k` | `k > 0`. In the UNION form, `Q_0` supplies at least `k` rows per input binding. In the direct LIMIT form, both regions retain `k` shared witness matches per eligible anchor. All branches project the same constant row `c`, preserving both values and multiplicity. |
| SC7 | SC | `[NOT] EXISTS { Q_L }` ↦ `[NOT] EXISTS { Q_H }` | EXISTS and NOT EXISTS retain at least one matching row on each side for every input binding. |
| SC8 | SC | `MATCH A WHERE FALSE MATCH L RETURN c` ↦ `MATCH A WHERE FALSE MATCH H RETURN c` | The empty mandatory input remains unchanged; no intervening aggregate or other clause recreates rows from that empty input. |
| SY1 | SY | `E_1 AND E_2 ↔ E_2 AND E_1` | Both expressions return Boolean-or-null values and evaluate deterministically without errors on the same input. Only their positions change; the expressions and graph remain unchanged. Commutativity does not require a false operand. |
| SY2 | SY | `E_1 OR E_2 ↔ E_2 OR E_1` | Both expressions return Boolean-or-null values and evaluate deterministically without errors on the same input. Only their positions change; the expressions and graph remain unchanged. Commutativity does not require a true operand. |
| SY3 | SY | `(u)-[:T]->(v) ↔ (v)<-[:T]-(u)`<br>or<br>`(u)-[:T]-(v) ↔ (v)-[:T]-(u)` | Preserve endpoint identities and constraints, relationship types, and direction meaning. Retain any bound relationship identity; observations of a named path must be invariant under reversal. |
| SY4 | SY | `p=(u)-[:T*1..b]-(v) ↔ p=(v)-[:T*1..b]-(u)` | The path is undirected and bounded (`b` is 2–4 in the current construction). Retain endpoint constraints and `all(x IN nodes(p) WHERE x.keep = true)`. Supported path observations are reversal-invariant, such as length and order-insensitive node predicates; direct observation of the path sequence is excluded. |
| SY5 | SY | `MATCH A MATCH B ↔ MATCH B MATCH A`<br>or<br>`MATCH A, B ↔ MATCH B, A` | Both matches are mandatory and independent: neither uses variables introduced by the other. The two components are either separate clauses or comma-separated parts of one `MATCH`. Preserve local constraints, output bindings, and row multiplicities. |
| SY6 | SY | `MATCH (u)-[:R]->(a) MATCH (u)-[:S]->(b)` ↔ `MATCH (u)-[:S]->(b) MATCH (u)-[:R]->(a)` | Both matches are mandatory and share only `u`. Neither branch depends on the other's private variables; endpoint constraints and row multiplicities remain unchanged. |
| SY7 | SY | `MATCH (u)-[:R]->(v) MATCH (v)-[:S]->(w)` ↔ `MATCH (v)-[:S]->(w) MATCH (u)-[:R]->(v)` | The two mandatory stages join through `v`. Each stage is valid in either position, with unchanged local constraints, endpoint bindings, and row multiplicities. |
| SY8 | SY | `MATCH A OPTIONAL MATCH B OPTIONAL MATCH C` ↔ `MATCH A OPTIONAL MATCH C OPTIONAL MATCH B` | The mandatory anchor `A` remains outside the exchange. The optional branches share only that anchor, have no cross-branch dependencies, and preserve null extension and row multiplicities. |

Here, `L` and `H` denote the low/high graph-match regions; `A`, `B`, and `C` denote matching components. Parenthesized UNION forms abbreviate the enclosing CALL and projection used to apply the outer limit. `[NOT]` denotes either unchanged use or unchanged omission of NOT. These entries describe the current Cypher catalog; individual backends may restrict the available forms. Gremlin implements SC1–SC8, SY1, SY2, and SY5–SY7; its SY5–SY7 exchange the two patterns of a declarative `match()` step. Potential-work differences are established by graph construction and must be checked against actual execution.

## Replacement versus operand exchange

SC1/SC2 use Boolean absorption to replace `E_L` with `E_H` while preserving the decisive guard and operand positions. The replaced expressions may return different values because the guard fixes the result of the enclosing expression.

SY1/SY2 use commutativity to exchange two unchanged expressions. The table states the general relation. The generator samples either `guard_swap`, with a false AND guard or a true OR guard, or `predicate_swap`, with two predicates from the same recipe over the low and high regions. Both predicates can be true in an AND pair.

The two groups vary different factors: protected work in SC1/SC2 and operand position in SY1/SY2. They can expose the same optimization defect and do not establish independent root causes. For example, eager evaluation of the graph-work operand can produce a size-dependent gap for SC1 while leaving both SY2 forms equally expensive. Issue deduplication therefore requires execution-plan and root-cause analysis.

## Case construction

After selecting a compatible host site, the generator chooses a candidate for that case. The expression and row-source candidates define both the query fragment and its graph region. For SY3–SY8 the candidate specifies the graph layout. The generator binds the symbols once, applies the transformation to a copy of the query and compares both queries on the same graph.

| Patterns | Candidate and graph construction |
| --- | --- |
| SC1/SC2, SY1/SY2 | One-hop count, two-hop count, or existence with no qualifying endpoint. Guards that read a graph witness use a separate region. |
| SC3/SC4 | Boolean slots admit all three expression candidates. Other scalar slots admit the two count candidates, converted to the result type. The selected CASE branch or non-null COALESCE prefix stays fixed. |
| SC5–SC8 | One-hop or two-hop sources project constant rows. Each pattern preserves its own demand, witness or empty-input condition as specified in Table 1. |
| SY3 | Concentrate edges on the selected endpoint, or keep one matching edge there and concentrate the additional edges on a node that fails the endpoint constraint. |
| SY4 | Retain a valid path and build additional bounded paths that fail the node predicate at a shared hub or at each terminal. |
| SY5 | Make the left or right component selective. It matches zero or one node while the other component gains N candidate nodes. |
| SY6 | Give one arm of the shared root one edge and the other N edges. Either arm can be wide. |
| SY7 | Retain a complete chain. The N extra right-hand edges start at distinct middle nodes or share a hub. None joins to the selected root. |
| SY8 | Keep four roots covering both branches, either branch alone, and neither branch. Expand the left or right branch on roots where it matches. |

The archive records the expression or row-source ID in `work_candidate` and the layout ID for SY3–SY8 in `graph_candidate`. N counts candidate-specific units, such as complete two-hop matches or extra right-hand edges. Execution profiles provide separate evidence of the work performed.

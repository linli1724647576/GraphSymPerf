# 3 Approach

GraphSymPerf tests graph database management systems (DBMSs) for missed optimizations through query pairs that preserve results while exposing contrasting amounts of potential execution work. Its design combines two testing opportunities. Equivalent representations of a graph match can lead to different access paths, expansion directions, or matching orders. Short-circuit contexts can make part of that work unnecessary once a decisive value or sufficient matches have been obtained. Building on SCor's cost-aware skeleton construction [1], GraphSymPerf couples these opportunities with graph states that make differences in execution work observable.

The workflow follows five steps. Skeleton construction defines symmetry-based and short-circuit-based templates with low-cost and high-cost placeholders. Graph state generation specifies \(G\) to realize these roles through selective access points, small neighborhoods, and larger matching regions. Query generation populates a selected skeleton \(S\) to obtain the base query \(Q_1\), then derives \(Q_2\) through symmetry transformation or reference substitution. Execution materializes \(G\), verifies its construction conditions, and runs the query pair. Finally, result and performance checking verifies the result relation and confirms substantial runtime differences through repeated measurements. Reduced cases and execution evidence are then used to investigate the additional work.

## 3.1 Skeleton Construction

A mutation pattern defines a result-preserving transformation and its applicability conditions. A query skeleton instantiates that pattern in an operator structure, with placeholders for labels, relationship types, predicates, and expressions. Each placeholder has a syntactic category and variable dependencies. Cost-bearing placeholders additionally receive low-cost or high-cost roles; structural placeholders specify labels, relationship types, property keys, and path bounds. Low-cost roles provide selective matches or inexpensive expressions; high-cost roles provide larger candidate sets or computations over them. Constraints on truth values, nullness, emptiness, and witness availability connect these cost roles to the required result relation.

Table 1 presents 23 mutation patterns, each with a separate identifier. P10 combines three forms of mandatory-match reordering; the remaining patterns are listed individually. Following metamorphic graph database testing [2], each pattern preserves the result relation on a fixed graph state. Patterns involving similar operators may construct different comparisons through changes in representation or reference substitution.

Throughout the method, \(L\) and \(H\) denote low-cost and high-cost components. For graph-based reference substitution, \(R\) is the smaller region corresponding to \(H\). Expressions \(E_H,E_R\) and query fragments \(Q_H,Q_R\) access these regions; \(Q_L\) denotes a low-cost query fragment. The symbol \(E\) denotes an expression, \(\phi\) a predicate, and \(C\) a condition; \(A,B\) denote matching fragments or expression branches. We write \(H\mapsto R\) for substitution of the designated relationship-type references, with the surrounding query structure preserved.

**Table 1. Mutation patterns.** SY rows show symmetry transformations; SC rows show short-circuit contexts for \(H\mapsto R\). All forms are schematic, read-only, and deterministic, and preserve the result multiset on \(G\). Unchanged bindings, predicates, and projections are retained.

| Pattern ID | Type | Transformation / context | Applicability condition |
|---|---|---|---|
| P1 | SY | `(u)-[:T]-(v)` ↔ `(v)-[:T]-(u)` | Undirected single edge; retain endpoint constraints. |
| P2 | SY | `(u)-[:FWD]->(v)` ↔ `(u)<-[:REV]-(v)` | Bijective inverse-edge pairing; relationship identity and properties unobserved. |
| P3 | SY | `path=(u)-[:T*m..n]-(v)` ↔ `path=(v)-[:T*m..n]-(u)` | Bounded undirected path; reversal-invariant path observations. |
| P4 | SY | `L AND E` ↔ `E AND L` | Boolean-or-null operands. |
| P5 | SY | `L OR E` ↔ `E OR L` | Boolean-or-null operands. |
| P6 | SY | `u.k = c` ↔ `c = u.k` | Property–literal equality. |
| P7 | SY | `(u:Label1:Label2)` ↔ `(u:Label2:Label1)` | Conjunctive labels on the same node. |
| P8 | SY | `[:T1\|T2]` ↔ `[:T2\|T1]` | The same alternative relationship types. |
| P9 | SY | `MATCH A WHERE φ MATCH B` ↔ `MATCH A MATCH B WHERE φ` | Mandatory clauses; `φ` uses only variables present in both patterns. |
| P10 | SY | `MATCH A MATCH B` ↔ `MATCH B MATCH A` | Independent clauses, shared-anchor branches, or a two-edge chain; local predicates. |
| P11 | SY | Exchange two `OPTIONAL MATCH` branches | Only the mandatory anchor is shared; no cross-branch dependencies. |
| P12 | SY | `Q_L UNION ALL Q_H` ↔ `Q_H UNION ALL Q_L` | Identical output columns; no branch-local limit. |
| P13 | SY | `CASE WHEN C THEN A ELSE B END` ↔ `CASE WHEN NOT coalesce(C,FALSE) THEN B ELSE A END` | Compatible branches; null-aware guard complement. |
| S1 | SC | `TRUE OR E_H` | Checked true operand; designated high-cost references occur only in `E_H`. |
| S2 | SC | `FALSE AND E_H` | Checked false operand; designated high-cost references occur only in `E_H`. |
| S3 | SC | `WHEN C THEN Q_L ELSE Q_H` | `C = TRUE` in this form; only the unselected query references `H`. |
| S4 | SC | `CASE WHEN C THEN L ELSE E_H END` | `C = TRUE` in this form; only the unselected expression references `H`. |
| S5 | SC | `MATCH H ... LIMIT 0`, or `MATCH H WHERE FALSE ...` | Zero output demand; no aggregate producing a row from empty input. |
| S6 | SC | `MATCH H RETURN c LIMIT n` | At least `n > 0` matches in each region; constant `c`; no `DISTINCT`. |
| S7 | SC | `coalesce(NULL, L, E_H)` | The prefix contains a checked non-null value. |
| S8 | SC | `EXISTS { MATCH H WHERE φ }` | Nonempty regions; every candidate satisfies `φ`. |
| S9 | SC | `MATCH A WHERE FALSE MATCH H ...` | Empty mandatory input; independent downstream match; no row-producing aggregate. |
| S10 | SC | `FALSE AND [NOT] EXISTS { MATCH H WHERE φ } RETURN DISTINCT ...` | Checked false guard; null-safe membership predicate `φ`. |

### 3.1.1 Symmetry-Based Skeletons

Symmetry-based skeletons preserve matched bindings or expression values while presenting alternative access and evaluation choices. Differences in selectivity, neighborhood size, and branch multiplicity provide contrasting candidate workloads for these representations.

**Endpoint and path symmetry.** P1 and P3 mirror undirected single-edge and bounded-path patterns while retaining each endpoint's labels and constraints. For P3, path length and universal predicates over individual path-node properties are invariant under reversal. These observations preserve the result while allowing the two representations to expose differences in expansion and predicate placement. P2 obtains a corresponding comparison through paired forward and inverse relationship types. Their one-to-one correspondence preserves endpoint bindings when the relationship direction and type are exchanged together.

**Matching-order symmetry.** Mandatory-match permutation (P10) has three variants: disjoint bindings, branches sharing only a bound anchor, and a chain of two fixed-length outgoing edges sharing only its connecting variable. Each variant uses clause-local predicates. Their respective dependencies determine which clauses may be exchanged. P11 permutes independent optional branches while preserving null extension. P9 relocates a predicate whose variables occur in both mandatory patterns, while P12 exchanges `UNION ALL` branches without changing duplicate multiplicities. These rules expose alternative matching and evaluation orders under their respective dependency constraints.

**Expression symmetry.** P4 and P5 exchange the operands of `AND` and `OR`, respectively. P6 exchanges equality operands, while P7 and P8 permute conjunctive node labels and alternative relationship types, respectively. P13 exchanges `CASE` branches and complements `coalesce(C,FALSE)`, preserving the selected value for true, false, and null guards [3]. These transformations expose representation sensitivity in the expressions and constraints associated with graph matching.

### 3.1.2 Short-Circuit-Based Skeletons

Short-circuit-based skeletons identify a point after which further computation cannot change the result. S1–S10 adapt SCor-style constructions [1] to graph-query contexts. Each pattern substitutes a smaller reference region for the designated high-cost region while preserving the facts observable by that context. The complete queries can therefore remain equivalent even when the substituted expressions return different intermediate values.

**Boolean absorption (S1, S2, S10).** S1 uses a true disjunct, while S2 uses a false conjunct. In each case, the decisive operand determines the Boolean result independently of the other operand. The decisive value may be a literal or an expression over controlled node properties. S10 applies Boolean absorption to a membership context: it combines a false guard with `DISTINCT` and a null-safe `EXISTS` or `NOT EXISTS` predicate.

**Unselected branches (S3, S4).** A checked condition selects a low-cost branch, leaving the other branch's value unobservable. S3 expresses this choice at query level through `WHEN`, whereas S4 uses a scalar `CASE` expression. The displayed forms use a true guard. The scalar variants also admit false or null guards, with the low-cost expression placed in the selected `ELSE` branch. Reference substitution changes only the unselected branch and preserves the selected value.

**Empty-result propagation (S5, S9).** S5 suppresses scan output through a zero limit or false filter. S9 places an independent high-cost match after an empty mandatory match. Both preserve an empty result under reference substitution, but locate the potentially unnecessary work differently: scan suppression for S5 and downstream suppression for S9.

**Non-null fallback (S7).** In `coalesce(NULL, L, E_H)`, a non-null `L` determines the returned value [4]. Replacing `E_H` with `E_R` preserves the result while reducing potential fallback work.

**Bounded demand and witnesses (S6, S8).** S6 requests a positive number of rows with identical constant projections. Both regions contain enough matches to satisfy the limit, preserving the result multiset regardless of which matches are returned. S8 instead preserves an existential witness in each region, exposing the opportunity to stop after the first qualifying match. Neo4j explicitly documents this early-termination behavior for existential subqueries [5].

## 3.2 Graph State Generation

Graph state generation realizes the cost roles of the selected skeleton through a finite construction recipe \(\rho\), determined by \(S\), \(p\), and the applicable state variant \(v\). A recipe specifies node and relationship groups, their multiplicities, endpoint connections, labels, and property values. Fixed anchors and small neighborhoods provide low-cost components \(L\); scalable groups provide the high-cost region \(H\). Their multiplicities depend on \(N\), while the facts required by the pattern remain fixed.

Pattern-specific additions preserve the relations used by query mutation. Inverse relationships are paired one-to-one with their forward counterparts through reversed endpoints and a shared pair key. When a reference region is required, \(R\) coexists with \(H\) under separately addressable relationship types. Its construction preserves required witnesses, bounded matching multiplicities, and matches needed when the low-cost component does not determine a branch. Algorithm 2 can then substitute the designated references while retaining the surrounding query structure.

The state variant controls where potentially unnecessary work occurs. An empty low-cost branch exposes downstream matching that could be suppressed; multiple low-cost matches at a shared anchor expose repeated evaluation. The P3 pruning variant places a node with `keep=false` before a large fan-out while retaining a successful two-hop path. All other nodes have `keep=true`. Under the required all-nodes `keep=true` condition, increasing the fan-out adds paths excluded by the failing node without removing the witness path. These variants modify prescribed recipes; the data seed \(k_G\) selects an identifier offset while retaining fixed anchor and witness keys.

Algorithm 1 constructs \(G\) as a compact specification of these groups and their metadata. It derives \(I\) from the specified populations, property assignments, and relationship correspondences. These are expected graph facts for later verification, rather than checks executed during generation. The output \((G,I)\) constrains skeleton population in Algorithm 2; materialization and verification occur in Section 3.4.

**Algorithm 1. Graph state generation.**

```text
Input:  skeleton S, selected pattern p, scale N,
        applicable state variant v, data seed k_G
Output: graph state specification G and construction conditions I

 1  ρ ← the prescribed construction recipe for S, p, and v
 2  if ρ is unavailable or N ≤ 0 then reject
 3  G ← an empty graph specification
 4  Add ρ's anchor groups with their fixed labels and access keys
 5  for each remaining node group U of L or H prescribed by ρ do
 6      n_U ← U's multiplicity determined by ρ, N, and v
 7      Append U to G with count n_U and its prescribed labels and typed properties
 8      Preserve required truth values, absent properties, and fixed witness nodes
 9  for each base relationship group F prescribed by ρ do
10      n_F ← F's prescribed multiplicity; append F with count n_F,
            type, endpoints, and properties
11  if p requires inverse relationships then
12      Add an inverse group with reversed endpoints for each designated forward group
13      Assign a common, pair-unique key to each forward–inverse edge pair
14  if a reference region is required then
15      Add R at the designated anchors using ρ's bounded reference groups
16      Preserve the required witnesses and bounded matching multiplicities
17      Retain matches needed by branches not decided by their low-cost component
18  Select an identifier offset using k_G
19  Assign identifiers within each group's prescribed range; retain all fixed keys
20  Record ρ, p, N, v, k_G, and the role bindings in G
21  Record the schema, property types, and required indexes in G
22  I ← label- and type-specific cardinalities and identifier-uniqueness conditions
23  Add the prescribed property values and witness-defining node and edge facts to I
24  Add the applicable inverse and reference correspondence conditions to I
25  return (G, I)
```

## 3.3 Query Generation and Mutation

Algorithm 2 consumes \((G,I)\) from Algorithm 1 and populates the same skeleton \(S\) for pattern \(p\). Imported matching fragments, when used, are incorporated into \(S\) before graph generation. The role bindings in \(G\) and the enclosing operator constrain expression selection.

Candidate pools vary predicate forms, correlation, access constraints, branch orders, and path bounds. An indexed-node constraint may use a property map or a `WHERE` predicate; a decisive Boolean value may use an equivalent comparison. Supported Boolean, `CASE`, and `COALESCE` wrappers further vary expression structure while preserving the required values.

For each placeholder \(x\), \(\mathcal{V}_x\) is its finite candidate pool, and \(\beta\) records the selected query assignments. The query seed \(k\) controls selection independently of \(k_G\), and \(d\) bounds additional equivalent wrapping.

**Algorithm 2. Skeleton population and query-pair construction.**

```text
Input:  skeleton S, selected pattern p, (G, I) from Algorithm 1,
        query seed k, additional wrapping bound d ∈ {0, 1, 2}
Output: test case (G, I, Q1, Q2, μ), with transformation record μ

 1  Q1 ← a copy of S; β ← ∅
 2  for each placeholder x of S in binding-dependency order do
 3      if x has a low-cost role then
 4          V_x ← permitted constants or forms over selective/small regions of G
 5      else if x has a high-cost role then
 6          V_x ← matching or expression forms over H
 7      else V_x ← structural choices prescribed by S for x
 8      Retain candidates satisfying x's kind, type, and scope,
            and compatible with the construction conditions in G and I
 9      if V_x = ∅ then reject
10      e ← a choice from V_x determined by k
11      Where permitted, wrap e in at most d equivalent expression forms
12      Q1 ← Q1[x ↦ e]; β[x] ← e; update variable bindings
13  if Q1 is ill-formed or p's applicability conditions fail then reject
14  Q2 ← an independent copy of Q1
15  if p has type SY then apply p's rewrite in Table 1 to Q2
16  else substitute H ↦ R in Q2's designated graph references
17  if Q2 is ill-formed or structurally identical to Q1 then reject
18  return (G, I, Q1, Q2, μ = (p, k, d, β))
```

Algorithm 2 carries \(G\) and \(I\) forward unchanged.

For example, S7 can be populated with a uniquely identified root and two outgoing relationship types. `HIGH` denotes the large region and `REF` the smaller reference region:

```cypher
// Q1
MATCH (u:Root {id: 1})
RETURN coalesce(NULL, 0,
  COUNT { MATCH (u)-[:HIGH]->(v) WHERE v.mark = true }) AS value

// Q2
MATCH (u:Root {id: 1})
RETURN coalesce(NULL, 0,
  COUNT { MATCH (u)-[:REF]->(v) WHERE v.mark = true }) AS value
```

Both queries return zero because the non-null prefix determines the result. Increasing `HIGH` changes the potential cost of evaluating the count while leaving the returned value unchanged.

For short-circuit patterns, \(Q_2\) is the reference query. The symmetry patterns P4, P5, and P13 additionally admit an auxiliary reference \(Q_{\mathrm{ref}}\), derived independently from \(Q_1\). These references retain edges needed for non-decisive guards and omit only work shielded by decisive values. Comparing \(Q_1\) and \(Q_2\) with \(Q_{\mathrm{ref}}\) can expose excessive work shared by both symmetry variants.

The catalog, role pools, and bounded nesting define a reproducible space of read-only queries, with availability determined by the target dialect. Within this space, coverage depends on distinct query structures and graph states, rather than seed counts alone. The generated pairs share the execution and checking procedure described next.

## 3.4 Execution and Checking

For each generated case \((G,I,Q_1,Q_2,\mu)\), the executor materializes \(G\), establishes the declared indexes, and checks \(I\) and index readiness. It refreshes planning information where supported before result checking, runtime screening, and independent confirmation. Both queries use the same graph state, database version, and configuration.

For a graph state \(G\), the required result relation is

\[
\llbracket Q_1\rrbracket_G =_{\mathrm{bag}} \llbracket Q_2\rrbracket_G.
\]

Complete results are compared as typed multisets, preserving nulls, duplicate multiplicities, and order within returned lists. Expected results derived during construction provide an additional check. When available, a reference query is checked first; an auxiliary \(Q_{\mathrm{ref}}\) is compared with both \(Q_1\) and \(Q_2\). Only completed, equal results proceed to ordinary performance comparison; errors and truncated outputs are recorded separately.

Runtime is measured from query submission through complete result consumption, with graph preparation and connection establishment outside the timed interval. After warm-up, measurements use balanced, shuffled AB/BA order. The initial screen considers the median paired runtime ratio and whether the same query is consistently slower. Candidates passing this screen enter two confirmation sessions, each with a fresh connection and a rebuilt, revalidated graph. Repeated pairs and A/A and B/B controls distinguish the observed gap from ordinary timing variation.

For an oriented pair, let \(r_i=t_{\mathrm{slow},i}/t_{\mathrm{fast},i}\) and \(\Delta_i=t_{\mathrm{slow},i}-t_{\mathrm{fast},i}\). Confirmation requires the median ratio and the lower endpoint of its bootstrap 95% confidence interval to meet their thresholds. The median runtime difference must exceed both an absolute threshold and the same-query noise bound, with consistent slowdown across the two sessions. The noise bound is the largest 99th percentile of absolute control-pair differences across the sessions. Thresholds and raw measurements are retained with each case. Timeouts receive separate outcomes and conservative bounds where a validated fast reference is available.

A confirmed slowdown becomes a candidate for diagnosis. We manually reduce its query and graph while preserving the result relation and runtime gap, then inspect execution plans and native work counters collected outside timing. This analysis distinguishes missed opportunities from expected effects of traversal or predicate order: equivalence alone does not require equal runtime. Reports are consolidated by root cause, separating the number of templates exercised from the number of distinct optimization problems. Database-specific manual probes are reported separately from cases generated by the pattern catalog.

## References

[1] Jinhui Lai et al. 2026. *Finding Missed Optimizations in DBMSs through Unbalanced Short-Circuit Query Construction.* Proceedings of the ACM on Management of Data 4(3), Article 184. [DOI: 10.1145/3802061](https://doi.org/10.1145/3802061).

[2] Shuang Liu et al. 2024. *Testing Graph Database Systems with Graph-State Persistence Oracle.* Proceedings of the 33rd ACM SIGSOFT International Symposium on Software Testing and Analysis (ISSTA '24). [DOI: 10.1145/3650212.3680311](https://doi.org/10.1145/3650212.3680311).

[3] Neo4j. *Cypher Manual: Conditional Expressions (CASE).* [Official documentation](https://neo4j.com/docs/cypher-manual/current/expressions/conditional-expressions/).

[4] Neo4j. *Cypher Manual: Scalar Functions — coalesce().* [Official documentation](https://neo4j.com/docs/cypher-manual/current/functions/scalar/#functions-coalesce).

[5] Neo4j. *Cypher Manual: EXISTS Subqueries.* [Official documentation](https://neo4j.com/docs/cypher-manual/current/subqueries/existential/).

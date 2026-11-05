# 4 Approach

This section presents GraphSymPerf. §4.1 gives an overview and the technical challenges. §4.2–§4.6 then describe the six steps of the workflow, and §4.7 shows why the constructed query pairs are equivalent and why their slowdowns reveal missed optimizations.

## 4.1 Overview

A test case of GraphSymPerf consists of a query pair $Q$, $Q'$ and a graph state $G$. We construct the case so that $Q$ and $Q'$ return the same result multiset on $G$, while one of them can be answered with much less work than the other. A graph DBMS that applies the expected optimization should therefore run both queries in similar time, and a stable slowdown indicates a missed optimization.

**Technical challenges.** This design realizes the key insight of §1: each pair encodes an explicit optimization opportunity. SCor [15] applies a related idea to relational DBMSs, drawing low-cost and high-cost items from tables that share one schema but differ in size. Realizing the insight on graph data raises three challenges that such a table-based construction does not face.

- **C1: Cost depends on the bound anchor.** Graph queries reach data by traversing from variables that the enclosing query has bound, as in the correlated subquery `COUNT { MATCH (v2)-->()-->() }`. A larger graph makes such an operand expensive only if the added edges are reachable from the nodes to which the anchor binds.
- **C2: Equivalence depends on the data.** A guard that reads the graph, such as an `EXISTS` test, fixes the result only if it holds for *every* binding that reaches the mutation point.
- **C3: Queries and data must agree on names.** The high-cost region appears only in $Q'$, yet it must exist in $G$; at the same time, the data added for the pattern must not be picked up by the rest of the query, or both queries slow down together.

GraphSymPerf addresses C1 by choosing anchors through scope analysis and placing the workload where they bind (§4.3, §4.5), C2 by installing witnesses on every candidate root (§4.5, Lemma 1), and C3 by binding all names at once in separate namespaces (§4.4). Beyond short-circuiting, it introduces *symmetry* patterns, which reorder interchangeable components and whose equivalence does not depend on the data (Lemma 2).

**Figure 4: Overview of GraphSymPerf.**

**Workflow.** Figure 4 shows the six steps of GraphSymPerf. ① A grammar-guided generator produces a random seed query whose AST records the type, labels and nullability of every variable (§4.2). ② GraphSymPerf selects a mutation pattern and a compatible mutation point, embeds the pattern there, and prepares a local edit. A short-circuit pattern protects a low-cost operand $L$ with a guard that already determines the result, and its edit replaces $L$ with a high-cost operand $H$; a symmetry pattern places a cheap and an expensive component in an order-insensitive construct, and its edit reorders them. Embedding also records which graph data the pattern needs (§4.3). ③ GraphSymPerf binds all symbols with one shared binding, instantiates the base query $Q$, and applies the edit to a copy of $Q$ to obtain $Q'$ (§4.4). ④ It then generates a graph state that keeps both queries equivalent and contains *few* matches for the low-cost side and *many* matches, scaled by $N$, for the high-cost side (§4.5). ⑤ Finally, it executes both queries on the graph DBMS under test and ⑥ reports a missed optimization only if their results agree and the slowdown is large and stable (§4.6). Algorithm 1 implements steps ①–③, and Algorithm 2 implements step ④.

**Running example.** We illustrate the approach with Figure 5, which builds two test cases from one seed query: one with the short-circuit pattern SC1 and one with the symmetry pattern SY6.

**Figure 5: Running example: mutations of the same seed query and their graph states.** (a) The seed query, its two mutation points, and the variable scope that determines which variables can serve as anchors. (b) SC1 replaces `v2.flag` with $D_T$ `OR` $E_L$ to obtain $Q$, and replaces $E_L$ with $E_H$ to obtain $Q'$. (c) SY6 replaces the first `MATCH` with two components $A$ and $B$ that share `v1`, and swaps them. In (b) and (c), the left column shows the query pair, the definitions of its parts and an excerpt of the binding θ, marking whether each name is used by $Q$, $Q'$ and $G$; the middle column shows the graph requirements `req`, from which Algorithm 2 builds the test graph on the right; the grey rows below its dashed line are the roots that Algorithm 2 resolves on $G_0$, and the numbers in `req` refer to the definitions on the left. In each graph, the background graph $G_0$ is grey and carries only seed labels, whereas the white nodes added by Algorithm 2 carry only pattern labels; the numbers (1)–(3) link each definition on the left to the edges it requires, and `{keep: true}` marks the end nodes that satisfy the omitted predicates `WHERE ·.keep`. $N \gg 1$ is the workload scale, and $Q(G)$ denotes the result multiset of $Q$ on $G$. Queries and graphs are shown with the symbols before binding, and labels, property keys and relationship types are abbreviated.

```text
Algorithm 1: Query Generation and Mutation
Input:  target graph DBMS with capability set B, mutation pattern p
Output: query pair (Q, Q′), symbol binding θ and graph requirements req,
        or ⊥ if p is not applicable
 1: // Step 1: Random AST generation
 2: T ← GenerateAST(B)                              ▷ names and constants stay symbolic
 3: // Step 2: Pattern embedding
 4: sites ← {s ∈ T | p is applicable at s}
 5: if sites = ∅ then return ⊥
 6: Select a mutation point s from sites at random.
 7: a ← SelectAnchor(variables visible at s or declared by T[s])
 8: if p is a short-circuit pattern then
 9:     (E_L, E_H) ← WorkOperands(p, a)             ▷ same shape, low- and high-cost regions
10:     D ← Guard(p, a)                             ▷ fixes the result for every binding of a
11:     F ← p.Form(D, E_L);  edit ← replace E_L with E_H
12: else                                            ▷ symmetry pattern
13:     F ← p.Form(T[s], a);  edit ← swap the two components of F
14: end if
15: Replace T[s] with F;  req ← p.Requirements(a, F)
16: // Step 3: Query generation and mutation
17: θ ← Bind(symbols of T, edit and req)            ▷ one binding for Q, Q′ and G
18: Q ← Instantiate(T, θ);  Q′ ← apply Instantiate(edit, θ) to a copy of Q
19: if not Consistent(Q, Q′) then return ⊥          ▷ scope, output columns, textual change
20: return (Q, Q′, θ, Instantiate(req, θ))

21: function Bind(Y)
22:     for each symbol y ∈ Y do
23:         if y is a label or relationship type then
24:             θ[y] ← Random_y if y comes from the seed query, else Pattern_y
25:         else if y is a property key then θ[y] ← y
26:         else θ[y] ← a random constant of the type of y   ▷ drawn once, shared by Q and Q′
27:         end if
28:     end for
29:     return θ
30: end function
```

## 4.2 Random AST Generation

The seed query supplies the context in which a pattern is embedded: the clauses around the mutation point, the variables in scope, and the outputs that observe the mutated value. GraphSymPerf generates it from the compact grammar in Figure 6. Labels, relationship types, property keys and constants (`ℓ`, `r`, `k`, `c`) remain typed symbols during generation and are bound only after embedding (§4.4), so that the queries and the graph state always agree on names.

**Figure 6: Compact syntax supported by GraphSymPerf.**

```text
Q    ::= C* RET | Q UNION [ALL] Q
C    ::= [OPTIONAL] MATCH P (, P)* [WHERE BE]
       | WITH IT (, IT)* [WHERE BE]
       | UNWIND LE AS id
       | CALL (id*) { Q }
RET  ::= RETURN [DISTINCT] IT (, IT)* [ORDER BY id+ [SKIP int] [LIMIT int]]
IT   ::= E AS id
P    ::= [id =] NP (RP NP)*
NP   ::= (id (:ℓ)+ [{k: c}]) | (id)
RP   ::= -[[id] :r [*1..int]]-> | <-[…]- | -[…]-
BE   ::= BE AND BE | BE OR BE | NOT BE | E COP E
       | E IS [NOT] NULL | EXISTS { SQ }
E    ::= id | id.k | c | BE | E AOP E
       | CASE WHEN BE THEN E ELSE E END
       | coalesce(E, E) | COUNT { SQ } | size(LE)
SQ   ::= Q | MATCH P WHERE BE
LE   ::= [E (, E)*] | COLLECT { Q }
COP  ::= = | <> | < | > | <= | >=
AOP  ::= + | - | *
```

A node pattern either introduces a fresh variable, which carries at least one label, or refers to an already bound variable, written `(id)`; every relationship pattern has a type. The namespace separation of §4.4 relies on these two restrictions.

**Generation.** `GenerateAST` (Algorithm 1, line 2) expands the grammar top-down from $Q$. At each step, it samples only among productions that are well typed, reference only variables in scope, and belong to the capability set $B$ of the target DBMS. $B$ records the dialect differences that affect generation, such as whether `CALL` may appear inside expression subqueries, whether a `WITH` clause keeps imported variables, and whether subqueries are written as full queries, as `MATCH` bodies or as pattern comprehensions. Generation limits $\Omega$ bound the expansion budget, the number of clauses and the nesting depth; once the budget is exhausted, only terminating productions remain.

During expansion, the generator tracks the scope of each AST node. `MATCH` declares new variables, `WITH` keeps only the projected names, together with the variables imported by an enclosing `CALL` if the capability set $B$ says that the target DBMS keeps them (as Neo4j does), and a direct alias such as `WITH v1 AS v2` inherits the labels and nullability of its source. `CALL` sees only the variables it imports and exposes only the columns it returns. To keep results deterministic, the generator precedes every `SKIP` or `LIMIT` with an `ORDER BY` over all scalar output columns and applies aggregates only to constants.

The seed query does not depend on the pattern, so the same seed query can host different patterns. Figure 5(a) shows a seed query generated in this way:

```cypher
MATCH (v1:U0:U1)
CALL (v1) {
  WITH v1 AS v2, v1.score AS v3
  RETURN v2.flag AS v4
}
RETURN v1 AS v5, v4 AS v6
```

**Gremlin.** For Gremlin, GraphSymPerf generates the linear subset of Figure 6, which excludes `UNWIND`, `CALL` and pagination, and renders it as traversal steps (Table 2). We present the approach in terms of Cypher; the Gremlin adaptation differs only in steps ① and ②.

**Table 2: Rendering of Figure 6 constructs in Gremlin.**

| Construct | Gremlin |
|---|---|
| `MATCH (x:ℓ)` | `g.V().hasLabel(ℓ)` |
| `MATCH (x)-[:r]->(y:ℓ)` | `.out(r).hasLabel(ℓ)` |
| `WHERE` | `.filter(·)` |
| `WITH x AS y` | `.as(y)`, read back with `.select(y)` |
| `CASE`, `coalesce` | `.choose(·)`, `.coalesce(·)` |
| `COUNT {…}` | `.count()` |

## 4.3 Pattern Embedding

**Patterns.** [Table 1](pattern-catalog.en.md) lists 16 mutation patterns in two families. *Short-circuit patterns* (SC1–SC8) rely on an unchanged guard or demand condition that fixes the result before a protected operand is evaluated; the mutation replaces this operand with a more expensive one. *Symmetry patterns* (SY1–SY8) rely on an algebraic symmetry such as commutativity; the mutation reorders two unchanged components. Each pattern accepts one kind of syntactic slot: Boolean expressions host SC1, SC2, SC7, SY1 and SY2; scalar expressions host SC3 and SC4; row sources with scalar output host SC5, SC6 and SC8; single-edge path patterns host SY3 and SY4; and mandatory `MATCH` clauses without `WHERE` host SY5–SY8.

Algorithm 1 accesses a pattern $p$ only through its slot, its applicability conditions from Table 1, a function `Form` that builds the fragment replacing the slot, and a function `Requirements` that derives the graph requirements; short-circuit patterns additionally provide candidate pools for their operands (`WorkOperands`) and guards (`Guard`). We say that $p$ is *applicable* at an AST node $s$ if $s$ fits the slot of $p$, $B$ supports every construct that $p$ introduces, and the context of $s$ satisfies the applicability conditions of $p$. For example, SY5–SY8 require that neither component uses variables introduced only by the other, and SY3 and SY4 require that the enclosing query does not observe the order of the path.

**Mutation point selection** (Algorithm 1, lines 4–6). For every AST node $s$, a scope analysis computes the variables visible at $s$ together with their types, label sources and nullability. GraphSymPerf keeps the nodes at which $p$ is applicable and samples one of them. Choosing by scope rather than by name matters. In Figure 5(a), the projection `v2.flag` is a Boolean slot for SC1 with `v1`, `v2` and `v3` in scope; `v1` remains visible after the `WITH` because the target DBMS keeps the imports of `CALL`. Although `v2` never appears in a labelled `MATCH`, it inherits `U0:U1` and non-nullability from `v1` through the alias and can therefore start a correlated path; `v3` is a float and cannot. A choice by name could pick `v3`, and a check limited to the labels of the current clause would miss `v2`. The first `MATCH` is a slot for SY6: its input scope is empty, but the clause itself declares `v1`, which the later `CALL (v1)` uses, so SY6 reuses `v1` as the shared root of its components.

**Anchor selection** (line 7). `SelectAnchor` chooses between a *correlated* fragment, which reuses a visible non-null node variable with known labels as its anchor so that its cost depends on the bindings produced by the seed query (C1), and an *uncorrelated* fragment, which starts from a fresh root and exercises subqueries that do not depend on outer bindings. When candidates exist, it chooses a correlated fragment with probability $p_c$; otherwise it uses a fresh root. Clause-level symmetry patterns such as SY6 always reuse the variables that the replaced clause declares.

**Short-circuit embedding** (lines 8–11). GraphSymPerf samples a work candidate, namely a one-hop count, a two-hop count, a non-matching `EXISTS` or a row source, and instantiates it twice from the anchor $a$: once on a low-cost region and once on a high-cost region. The resulting operands $E_L$ and $E_H$ have the same shape, but their relationship types and end labels belong to disjoint regions. The guard $D$ must have the same value for every binding of $a$; it is a literal, a constant comparison, a count or existence test over a separate witness region, or a property of the anchor wrapped in `coalesce`. The embedded form $F$ uses $E_L$, and the edit replaces $E_L$ with $E_H$. For instance, SC1 rewrites a Boolean expression and SC4, the pattern of the motivating example in Figure 2, rewrites a scalar expression:

$$BE \;\rightarrow\; D_T \;\texttt{OR}\; E_L, \qquad E \;\rightarrow\; \texttt{coalesce}(\texttt{NULL},\, V,\, E_L),$$

$$E_X \;::=\; \texttt{COUNT}\,\{\texttt{MATCH}\,(a)\text{-}[{:}r_X]\text{->}()\text{-}[{:}r'_X]\text{->}(t)\;\texttt{WHERE}\;t.\texttt{keep}\} > 0 \;\mid\; \cdots, \qquad X \in \{L, H\},$$

where $D_T$ is a guard that is always true and $V$ is an unchanged non-null value. In Figure 5(b), SC1 uses the anchor `v2`, an existence guard and a two-hop count. Here and in Figure 5, we omit the labels of the fresh nodes `w`, `t` and the unnamed middle nodes; each of them carries a label from the pattern namespace (§4.4):

```text
D_T = EXISTS { MATCH (v2)-[:W]->(w) WHERE w.keep }
E_X = COUNT  { MATCH (v2)-[:X1]->()-[:X2]->(t) WHERE t.keep } > 0      X ∈ {L, H}
F   = D_T OR E_L                                                      replaces v2.flag
```

**Symmetry embedding** (lines 12–13). The form reuses the components at the mutation point or extends them with fresh ones, and the edit reorders two components without changing either. In Figure 5(c), SY6 replaces the first `MATCH` with two mandatory components that share only `v1`; the property `{anchor: 1}` restricts the root to a single node:

```text
A = MATCH (v1:U0:U1 {anchor: 1})-[:R]->(y)
B = MATCH (v1)-[:S]->(z)
F = A B,  edit = swap A and B
```

**Graph requirements** (line 15). Embedding also records what the graph state must contain. The requirements `req` consist of the anchor, whose labels identify the candidate roots; the *witnesses* that every candidate root needs so that the guard keeps its value; and a *cost layout*, which states whether one root or every root carries the workload and, for each such root, a *fixed part* built once and a *unit* repeated in proportion to $N$. The middle columns of Figure 5(b) and (c) show `req` for the running example, and Table 4 (§4.5) lists the requirements of all patterns.

**Gremlin.** GraphSymPerf supports SC1–SC8, SY1, SY2 and SY5–SY7 for Gremlin. Short-circuit patterns and SY1/SY2 are embedded at `filter`, `map` or final traversal steps; SY5–SY7 insert a declarative `match()` step with two patterns after the root `V().hasLabel(·)` and exchange those patterns. Because a traverser carries a single current object, the anchor is either that object or a node named by an earlier `as(·)` step and reached through `select(·)`, so every Gremlin fragment is correlated. The work operands are one-hop traversals such as `out(r_X).has('keep', true)`, and the short-circuit edit replaces the relationship type $r_L$ with $r_H$.

## 4.4 Query Generation and Mutation

**Symbol binding** (Algorithm 1, line 17; function `Bind`, lines 21–30). Up to this point, every name and constant in the embedded AST, the edit and the graph requirements is a symbol. GraphSymPerf binds all of them at once with a single map $\theta$, rather than drawing names from an existing schema or binding each artifact separately. Labels and relationship types receive a name in one of two namespaces, one for the seed query and one for the pattern; property keys keep their names; and constants are drawn once by type. Table 3 shows part of $\theta$ for SC1 and where each name is used; Figure 5 shows an excerpt of the same binding below each query pair.

**Table 3: Part of the symbol binding for SC1 in Figure 5.**

| Symbol | Owner | Bound name | $Q$ | $Q'$ | $G$ |
|---|---|---|:-:|:-:|:-:|
| `U0`, `U1` | seed | `Random_label_0`, `Random_label_1` | ✓ | ✓ | ✓ |
| `flag`, `score` | seed | `property_bool`, `property_float` | ✓ | ✓ | ✓ |
| `W` | pattern | `Pattern_p_witness_edge` | ✓ | ✓ | ✓ |
| `L1`, `L2` | pattern | `Pattern_p_low_first_edge`, `Pattern_p_low_edge` | ✓ | | ✓ |
| `H1`, `H2` | pattern | `Pattern_p_high_first_edge`, `Pattern_p_high_edge` | | ✓ | ✓ |
| labels of `w`, `m`, `t` | pattern | `Pattern_p_witness`, `Pattern_p_low_middle`, `Pattern_p_low`, … | ✓ | ✓ | ✓ |

This single binding provides two guarantees on which the rest of the approach relies.

*Shared binding.* $Q$, $Q'$ and $G$ use the same name for every symbol, including symbols that only some of them mention. In Table 3, the high-cost types `H1` and `H2` never occur in $Q$, yet §4.5 must build $N$ paths of exactly these types for $Q'$ to traverse; conversely, `L1` and `L2` occur in $Q$ but not in $Q'$. Binding only the base query would leave the names of $Q'$'s operand undefined, and binding the artifacts separately could let $Q'$ reference types that $G$ does not contain. Because the edit is bound together with the AST before mutation, applying it introduces no new names or constants either, so $Q$ and $Q'$ differ only at the mutation point.

*Namespace separation.* Seed labels and relationship types are disjoint from pattern labels and relationship types. Since every fresh node pattern carries a label and every relationship pattern a type (§4.2), the seed query can reach the data added for the pattern only through the paths that the pattern itself writes. Suppose instead that the high-cost end nodes carried the seed labels `U0:U1`: the seed pattern `MATCH (v1:U0:U1)` would also bind `v1` to these $N$ nodes, both queries would change in result and cost, and the cost difference would no longer be confined to the mutation point. Property keys are not separated; a pattern marker such as `keep` may also occur on background nodes and in seed predicates. This is harmless, because a seed predicate can reach a pattern node only through a pattern label or type.

**Instantiation and mutation** (lines 18–19). GraphSymPerf instantiates the embedded AST into the base query $Q$ and applies the bound edit to a copy of $Q$ to obtain $Q'$; the short-circuit mutation replaces $E_L$ with $E_H$, and the symmetry mutation reorders the two components of $F$. A static check then requires both queries to satisfy the scope rules of §4.2, to expose the same output columns, and to differ in text. Whether their results are equal is decided by execution (§4.6). Figure 5 shows the resulting pairs.

## 4.5 Graph State Generation

Algorithm 2 builds the graph state $G$ from the bound requirements. It answers two questions separately: which roots must satisfy the equivalence constraints, which determines where witnesses go, and which roots carry the workload, which determines where the cost concentrates.

```text
Algorithm 2: Graph State Generation
Input:  query pair (Q, Q′), symbol binding θ, graph requirements req, workload scale N
Output: graph state G on which Q and Q′ are equivalent, or ⊥
 1: // Step 1: Background graph
 2: G ← RandomGraph(labels, types and keys of the seed query, bound by θ)
 3: // Step 2: Candidate and workload roots
 4: roots ← nodes of G that carry every label of the anchor, in creation order
 5: if roots = ∅ then roots ← [a new node with the anchor's labels]
 6: if req.layout = PerRoot then                    ▷ short-circuit patterns
 7:     workRoots ← roots
 8: else                                            ▷ symmetry patterns
 9:     workRoots ← [first node of roots]
10: end if
11: if req needs a unit on every workload root and N < |workRoots| then return ⊥
12: // Step 3: Equivalence constraints
13: for each root r in roots do
14:     for each witness w in req.witnesses do
15:         AddPath(G, r, w)                        ▷ fixes the guard for every binding
16:     end for
17: end for
18: // Step 4: Cost regions
19: (q_1, …, q_k) ← Split(N, k = |workRoots|)       ▷ near-equal shares that sum to N
20: for i ← 1 to k do
21:     h ← AddPath(G, workRoots[i], req.fixed)     ▷ built once: low-cost side
22:     for j ← 1 to q_i do
23:         AddPath(G, h, req.unit)                 ▷ one high-cost match
24:     end for
25: end for
26: // Step 5: Schema registration
27: Register the labels, types and keys of Q, Q′ and G in the DBMS schema.
28: return G

29: function AddPath(G, u, f)
30:     Create the nodes, edges and properties of fragment f, starting from node u.
31:     return the node of f to which later fragments attach
32: end function
```

**Background graph** (lines 1–2). `RandomGraph` creates $n_0$ nodes and $m_0$ edges over the labels, relationship types and property keys of the seed query and assigns random property values. It does not solve the predicates of the seed query; it only provides a context that is independent of the pattern. Because it uses seed names only, no background node carries a pattern label (§4.4). In Figure 5, $G_0$ consists of two unconnected nodes `n0` and `n1` labelled `:U0:U1`, with `flag` set to `false` and `true`.

**Candidate and workload roots** (lines 3–11). The candidate roots are the nodes that carry all labels of the anchor; if there are none, a new root is created. Filtering by label alone over-approximates the inputs, since other predicates of the seed query may stop some candidates from reaching the mutation point. The workload roots are all candidates for short-circuit patterns and only the first for symmetry patterns. SY8 is the one exception: it adds three roots next to the first, so that the four roots cover the cases in which both, one or neither of its optional branches match (Table 4). Line 11 rejects a case whose pattern needs at least one unit on every workload root but whose $N$ is smaller than the number of such roots; for example, SC7 needs a high-cost match on every root so that `EXISTS` keeps its value.

**Equivalence constraints** (lines 12–17). Witnesses go to every candidate root, not only to the workload roots: the anchor may bind to any candidate, and a witness on one root cannot fix the guard for another. Symmetry patterns have no witnesses (Lemma 2).

**Cost regions** (lines 18–25, 29–32). `Split` divides $N$ into near-equal integer shares over the workload roots. For each workload root, line 21 builds the fixed part once, and lines 22–24 attach $q_i$ units to the node `h` that the fixed part returns. Only the units count towards $N$. Because `h` can be an inner node, a two-hop high-cost region creates its middle node once, and each further match adds a single edge from it. A fragment may also consist of properties only, such as `anchor = 1` in SY6.

**Schema registration** (lines 26–27). Finally, Algorithm 2 declares the labels, relationship types and property keys of both queries and of $G$ in the DBMS schema, so that empty regions are declared as well.

**Running example.** The trace below runs Algorithm 2 on Figure 5. Both anchors resolve to `U0:U1`, so both cases have the candidate roots `[n0, n1]`. SC1 needs the `W` witness on both roots, because `v2` can bind to `n0` in one call and to `n1` in another, and a witness on `n0` cannot make the `EXISTS` true for `n1` (marker (1) in Figure 5(b)); markers (2) and (3) are the low-cost path and the high-cost region. SY6 places its whole workload on `n0` (markers (1) and (2) in Figure 5(c)), while `n1` contributes no rows in either join order.

```text
Trace on Figure 5.   G₀ = { n0:U0:U1 {flag: false},  n1:U0:U1 {flag: true} }

SC1   req.anchor    = v2 : U0:U1                 req.layout = PerRoot
      req.witnesses = [ (r)-[:W]->(w {keep}) ]
      req.fixed     = (r)-[:L1]->()-[:L2]->(t {keep}),  (r)-[:H1]->(m_H)    ▷ returns m_H
      req.unit      = (m_H)-[:H2]->(t {keep})

      roots = [n0, n1]     workRoots = [n0, n1]     q = [N/2, N/2]
      n0 :  W-witness   L1-L2 path   H1 to m_H   N/2 × H2 from m_H
      n1 :  W-witness   L1-L2 path   H1 to m_H   N/2 × H2 from m_H
      Q(G) = Q′(G) = {(n0, true), (n1, true)}     E_L reads 2 paths, E_H reads N

SY6   req.anchor    = v1 : U0:U1                 req.layout = OneRoot
      req.witnesses = [ ]
      req.fixed     = r.anchor ← 1,  (r)-[:R]->(y)                        ▷ returns r
      req.unit      = (r)-[:S]->(z)

      roots = [n0, n1]     workRoots = [n0]         q = [N]
      n0 :  anchor = 1   one R edge   N × S edge
      n1 :  nothing
      Q(G) = Q′(G) = N × (n0, false)              first MATCH: 1 row in Q, N rows in Q′
```

Table 4 summarizes the requirements of all patterns. Short-circuit patterns spread the workload over every candidate root, because the guard must hold for every binding; symmetry patterns concentrate it on one root, because their equivalence needs no data.

**Table 4: Graph requirements of the mutation patterns.**

| Patterns | Witnesses on every candidate root | Workload roots | Fixed part per workload root | Unit |
|---|---|---|---|---|
| SC1–SC4, SY1, SY2 | one match if the guard or non-null prefix reads the graph | all | one low-cost match and the shared prefix of the high-cost matches | one high-cost match |
| SC5, SC8 | none (zero demand or empty input) | all | one low-cost row | one high-cost row |
| SC6 | $k$ constant rows | all | one low-cost row | one high-cost row |
| SC7 | none, but at least one unit per root | all | one low-cost row | one high-cost row |
| SY3 | none | first | the endpoint constraint on the root | one edge from the root, or from a node that fails the endpoint constraint |
| SY4 | none | first | one valid bounded path | one bounded path that fails the node predicate at a shared hub or at its end |
| SY5 | none | first | a selective component matching at most one node | one node of the other component |
| SY6 | none | first | the narrow arm (one edge) | one edge of the wide arm |
| SY7 | none | first | one complete chain | one right-hand edge that does not join the root |
| SY8 | none | four roots covering both, either and neither branch | one narrow-branch edge on each root where it matches | one wide-branch edge on a root where it matches |

## 4.6 Execution and Performance Checking

GraphSymPerf loads $G$ once per test case and does not modify it during measurement.

**Result check.** GraphSymPerf first executes $Q$ and $Q'$ and compares their outputs as typed result multisets, including column names and row multiplicities. Values are normalized by type before comparison, so that null, Booleans, integers, floating-point numbers and strings never compare equal across types, and graph entities are compared by identity. Under the assumptions of §4.7, $Q$ and $Q'$ are equivalent on $G$; a mismatch therefore indicates either a logic bug in the DBMS or a violated assumption, and it is reported as a result inconsistency rather than a performance issue. Every later execution is checked against the same reference result.

**Timing.** GraphSymPerf measures the pair in two phases, *discovery* and *confirmation*. A phase consists of $w$ untimed warm-up rounds followed by $n$ timed rounds; each round runs both queries once, and consecutive rounds alternate between the orders $QQ'$ and $Q'Q$ so that caching favours neither query. Let $t$ and $t'$ be the median times of $Q$ and $Q'$ in a phase, and call the query with the larger median the slower one. The phase passes if

$$\frac{\max(t, t')}{\min(t, t')} \ge \rho, \qquad \operatorname{median}_i\,(\text{slower}_i - \text{faster}_i) \ge \delta, \qquad \frac{\lvert\{i : \text{slower}_i > \text{faster}_i\}\rvert}{n} \ge \kappa.$$

The ratio threshold $\rho$ requires a large relative gap, the absolute threshold $\delta$ discards millisecond-scale gaps that noise dominates, and the consistency threshold $\kappa$ requires the same query to be slower in most rounds. The last condition is a one-sided sign test: if both queries had the same running-time distribution, each round would favour either query with probability $1/2$, and a fixed query would be slower in at least $\lceil \kappa n \rceil$ of $n$ rounds with probability

$$p(n, \kappa) \;=\; \sum_{i = \lceil \kappa n \rceil}^{n} \binom{n}{i}\, 2^{-n}.$$

**Performance checking.** A pair that passes discovery ($n = n_d$) is measured again in a longer confirmation phase ($n = n_c > n_d$). GraphSymPerf reports a *stable slowdown* only if confirmation also passes and identifies the same slower query, which bounds the chance of reporting pure noise by $p(n_c, \kappa)$. Queries that exceed the per-query timeout $\tau$ are recorded separately. For every pair whose timing completes, GraphSymPerf collects the profiled execution plans of both queries, together with the per-root allocations of §4.5; we reduce stable candidates and use this evidence to identify the work that the DBMS fails to avoid.

A stable slowdown has a different meaning for each family. For a short-circuit pair, a slower $Q'$ indicates that the DBMS evaluates the protected operand although the guard already determines the result. For a symmetry pair, a slowdown in either direction indicates that the plan depends on the syntactic order of two interchangeable components. §4.7 explains why such a slowdown reflects a missed optimization rather than an artifact of the constructed data.

## 4.7 Correctness

This section states the assumptions behind the construction, shows that they make $Q$ and $Q'$ equivalent on $G$, and explains why a stable slowdown between them reveals a missed optimization.

**Definition 1 (Equivalent query pair).** $Q$ and $Q'$ are *equivalent on* $G$, written $Q \equiv_G Q'$, if they expose the same output columns and $Q(G) = Q'(G)$ as bags of rows.

**Assumptions.** GraphSymPerf relies on three assumptions.

- **A1 (Determinism).** $Q$ and $Q'$ are read-only, and because the generator removes order-dependent outputs (§4.2), each denotes a single bag on $G$. Query semantics is compositional: replacing a subexpression or clause with one that yields the same value, or the same bag, for every input binding preserves the result.
- **A2 (Total operands).** Every operand that a pattern protects, replaces or reorders evaluates without a runtime error to a Boolean, an integer or `null`. This holds by construction, since these operands are built from literals, constant comparisons, `COUNT` and `EXISTS` subqueries over read-only patterns, type conversions of counts, and `coalesce`.
- **A3 (Label-sourced anchors).** Every binding of the anchor at the mutation point is a node that carries the anchor's labels. The anchor is non-null, and its labels come from a `MATCH` pattern (a `hasLabel` step in Gremlin) or from an alias of a matched variable; a fresh root is matched by its own label. Hence every binding of the anchor is a candidate root in Algorithm 2 (line 4).

**Lemma 1 (Short-circuit equivalence).** Let $p$ be a short-circuit pattern and let $G$ be produced by Algorithm 2. Under A1–A3, $Q \equiv_G Q'$.

*Proof sketch.* Let $\eta$ be any input binding that reaches the mutation point. By A3, $\eta$ binds the anchor to a candidate root, and Algorithm 2 installs the witnesses of the guard $D$ on every candidate root (lines 12–17); literal, constant-comparison and `coalesce`-wrapped guards do not depend on the graph at all. Hence $D$ takes the same decisive value under every $\eta$: `true` for `OR` and `CASE`, and `false` for `AND`. As discussed in §2.4, a decisive operand fixes the value of `OR` and `AND` for every operand that is free of side effects and errors, including one that evaluates to `null`, and by A2 the protected operand is such an operand. Thus $F$ and its edited form yield the same value under every $\eta$. The remaining short-circuit patterns follow the same argument with their own decisive condition: the first true `WHEN` of SC3, the non-null `coalesce` prefix of SC4, the unchanged demand of SC5, SC6 and SC8 (`LIMIT 0`, $k$ identical witness rows before `LIMIT k`, or an empty mandatory input), and, for SC7, a match in both regions on every candidate root, which line 11 of Algorithm 2 enforces. By A1, equal values at the mutation point give $Q(G) = Q'(G)$. $\square$

Lemma 1 holds only on graphs that contain the witnesses, which is exactly why Algorithm 2 places witnesses on every candidate root (C2).

**Lemma 2 (Symmetry equivalence).** Let $p$ be a symmetry pattern. Under A1 and A2, $Q(G') = Q'(G')$ for *every* graph $G'$.

*Proof sketch.* For SY1 and SY2, `AND` and `OR` are commutative in three-valued logic, and by A2 neither operand raises an error in either position. For SY5–SY7, the two mandatory `MATCH` clauses carry no `WHERE` and neither uses variables introduced only by the other. Cypher enforces relationship uniqueness within a single `MATCH` clause, so each clause yields the same set of matches in either position, and the result is the bag join of these matches on their shared variables, which is commutative. In the comma-separated form of SY5, both parts belong to one `MATCH`, so the uniqueness rule ranges over the same relationships in either order. Labels and properties attached to a component, such as `{anchor: 1}` in SY6, move with that component. For SY8, both `OPTIONAL MATCH` branches depend only on the mandatory root, and left outer joins with mutually independent right sides commute, including their `null` extensions. For SY3 and SY4, reversing a path pattern yields the same bindings of its node and relationship variables, and GraphSymPerf rejects the case if the enclosing query observes the order of the path. By A1, $Q(G') = Q'(G')$. $\square$

Because Lemma 2 holds for every graph, symmetry patterns need no witnesses; the graph state only shapes their cost.

**From equivalence to missed optimizations.** An equivalent pair alone does not show that a slowdown is a defect, because the DBMS might lack any correct way to avoid the extra work. We therefore report a pair only when its slower query admits an evaluation strategy that is correct on *every* graph and, on $G$, does no more work than the faster query. Both families satisfy this condition.

For short-circuit pairs, the decisive operand precedes the protected one in written order in every form that has one: the guard of `OR` and `AND`, the first `WHEN` of `CASE`, the non-null prefix of `coalesce`, the witness branch of `UNION ALL`, and the empty `MATCH` of SC8. Even an engine that evaluates strictly from left to right can therefore skip the protected operand once the decisive one is known (§2.4), and the demand-based forms can stop consuming rows once their `LIMIT` is met or their `EXISTS` test finds a row. This per-binding decision is correct on every graph. Since the guard is either a constant or a one-hop witness test, skipping $E_H$ makes the cost of $Q'$ match that of $Q$ on $G$. For symmetry pairs, Lemma 2 implies that the plan chosen for the faster query is a correct plan for the slower one on every graph, so a planner that treats both forms alike would give them the same cost.

In both cases, the faster strategy does not depend on the data in $G$; $G$ only makes the cost of not using it visible, and the stable-slowdown criterion of §4.6 rules out gaps caused by noise. Strategies of both kinds are implementable in practice: Neo4j documents that an existential subquery stops after its first row [22], and ArcadeDB developers fixed missed opportunities of both families by evaluating `coalesce` arguments incrementally [2] and by reordering independent pattern components [1]. Whether developers consider a particular strategy worth implementing remains their decision; GraphSymPerf reports each pair with both execution plans so that it can be judged.

**Limitations.** Candidate roots over-approximate the anchor bindings, so some workload may be placed on roots that never reach the mutation point; this can cause a missed detection but not a false report. Namespace separation does not hide pattern data from scans without a label or type restriction; such scans read the same $G$ in both queries, so they preserve equivalence but may dilute the cost ratio.

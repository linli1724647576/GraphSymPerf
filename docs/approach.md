# Method

## System Overview

GraphSymPerf constructs equivalent query pairs around explicit optimization opportunities. Short-circuit mutations replace a low-cost expression or query fragment in a short-circuited position with a high-cost counterpart, testing whether the graph database management system (GDBMS) avoids evaluating it. Symmetry mutations change the representation of equivalent graph matching constraints to test access-path and matching-order selection. The workflow comprises four steps.

**Step 1: Patterns and Skeleton Construction.** GraphSymPerf defines short-circuit and symmetry patterns that specify the targeted optimization opportunities and their applicability conditions. It encodes each pattern's source and transformed forms as a parameterized query skeleton with low-cost ($L$) and high-cost ($H$) placeholders. For example, a short-circuit skeleton changes `coalesce(NULL, 0, L)` to `coalesce(NULL, 0, H)`: the earlier non-null zero determines both results, while the potentially unnecessary argument becomes more expensive.

**Step 2: Query Generation and Mutation.** GraphSymPerf generates a random host abstract syntax tree (AST), selects a compatible site using its scope table, and embeds the source form of one pattern. It prepares the local mutation and the graph requirements together. Names and constants are final when created.

**Step 3: Graph State Generation.** GraphSymPerf constructs a graph state $G$ containing the pattern's fixed witnesses and expandable matching regions. It applies the prepared mutation to a copy of the embedded query to obtain $Q'$. Both queries use the same graph and indexes, and the constructed data satisfies the pattern's equivalence conditions.

**Step 4: Result and Performance Checking.** GraphSymPerf executes the pair on the target GDBMS using the same graph state, verifies result-multiset equality, and checks for stable slowdowns through repeated measurements. The resulting candidates are further investigated using reduced cases, execution plans, and work counters to identify the missed optimization responsible for the additional work.

## Patterns and Skeleton Construction

Detecting missed optimizations requires queries that expose an applicable optimization opportunity and make the cost of missing it observable. To generate such queries systematically, GraphSymPerf organizes query patterns around concrete optimization mechanisms in GDBMSs, including short-circuit evaluation, early termination, and matching-order selection. Table 1 summarizes 8 short-circuit patterns (SC1–SC8) and 8 symmetry patterns (SY1–SY8), together with the semantic conditions under which each applies. Short-circuit patterns increase the potential cost of a component that can be skipped, while symmetry patterns expose alternative access paths and matching orders for equivalent graph queries. We encode these patterns as query skeletons with contrasting cost roles so that unnecessary computation or an inefficient execution order can produce a measurable performance gap.

**Notation and Applicability Conditions.** Throughout this section, $L$ and $H$ denote components with low-cost and high-cost roles, respectively. The pairs $E_L$, $E_H$ and $Q_L$, $Q_H$ denote low-cost and high-cost expressions and query fragments, respectively. Outside the transformed fragment, the patterns retain the original bindings, predicates, and projections. They apply only to read-only, deterministic, well-typed computations without evaluation errors. Their conditions must hold for every relevant outer binding, including when a pattern is embedded in a subquery. Order-sensitive consumers require an additional invariance check; equality here means equality of complete result multisets, including duplicates, nulls, and the order within returned lists.

**Table 1: Mutation patterns and their applicability conditions.** SC and SY denote short-circuit and symmetry patterns.

| ID | Type | Transformation | Applicability condition |
|---|---|---|---|
| SC1 | SC | `D_T OR E_L ↦ D_T OR E_H` | Unchanged true guard; Boolean-or-null replacements. |
| SC2 | SC | `D_F AND E_L ↦ D_F AND E_H` | Unchanged false guard; Boolean-or-null replacements. |
| SC3 | SC | `CASE WHEN D_T THEN V ELSE E_L END ↦`<br>`CASE WHEN D_T THEN V ELSE E_H END`<br>(or `E_L ↦ E_H` in a later `WHEN` condition) | The first true condition and its result are unchanged; compatible result types; a later-condition replacement is Boolean-or-null. |
| SC4 | SC | `coalesce(NULL, V, E_L) ↦`<br>`coalesce(NULL, V, E_H)` | `V` is unchanged and non-null (possibly `false` or 0); compatible argument types. |
| SC5 | SC | `MATCH L RETURN c LIMIT 0 ↦`<br>`MATCH H RETURN c LIMIT 0` | Both forms keep the zero limit; read-only replacement. |
| SC6 | SC | `(Q_0 UNION ALL Q_L) LIMIT k ↦`<br>`(Q_0 UNION ALL Q_H) LIMIT k`<br>(or `MATCH L RETURN c LIMIT k ↦ MATCH H RETURN c LIMIT k`) | `k > 0`; `Q_0` supplies at least `k` rows per input binding, or both regions share `k` witness matches per anchor; all branches project the same constant row `c`. |
| SC7 | SC | `[NOT] EXISTS { Q_L } ↦ [NOT] EXISTS { Q_H }` | Both sides match for every input binding. |
| SC8 | SC | `MATCH A WHERE FALSE MATCH L RETURN c ↦`<br>`MATCH A WHERE FALSE MATCH H RETURN c` | Same empty mandatory input; no intervening operator, such as an aggregate, that produces a row from empty input. |
| SY1 | SY | `E_1 AND E_2 ↔ E_2 AND E_1` | Boolean-or-null, deterministic, error-free operands; operands unchanged. |
| SY2 | SY | `E_1 OR E_2 ↔ E_2 OR E_1` | Boolean-or-null, deterministic, error-free operands; operands unchanged. |
| SY3 | SY | `(u)-[:T]->(v) ↔ (v)<-[:T]-(u)`<br>(or `(u)-[:T]-(v) ↔ (v)-[:T]-(u)`) | Endpoint identities, constraints, and edge direction preserved; a bound relationship refers to the same edge; reversal-invariant observations of a named path. |
| SY4 | SY | `p=(u)-[:T*1..b]-(v) ↔`<br>`p=(v)-[:T*1..b]-(u)` | Undirected bounded path; endpoint and all-node constraints retained; only reversal-invariant observations of `p`. |
| SY5 | SY | `MATCH A MATCH B ↔ MATCH B MATCH A`<br>(or `MATCH A, B ↔ MATCH B, A`) | Mandatory, independent components; neither uses variables introduced by the other. |
| SY6 | SY | `MATCH (u)-[:R]->(y) MATCH (u)-[:S]->(z) ↔`<br>`MATCH (u)-[:S]->(z) MATCH (u)-[:R]->(y)` | Mandatory branches that share only `u`. |
| SY7 | SY | `MATCH (u)-[:R]->(v) MATCH (v)-[:S]->(w) ↔`<br>`MATCH (v)-[:S]->(w) MATCH (u)-[:R]->(v)` | Two mandatory stages joined through `v`; each stage is valid in either position. |
| SY8 | SY | `MATCH A OPTIONAL MATCH B OPTIONAL MATCH C ↔`<br>`MATCH A OPTIONAL MATCH C OPTIONAL MATCH B` | The mandatory anchor `A` is not exchanged; optional branches share only `A`; null extensions preserved. |

`D_T`/`D_F` are unchanged guards that are true/false for every eligible input binding. `E_L`/`E_H` and `Q_L`/`Q_H` are low-/high-cost expressions and queries; `L`/`H` are low-/high-cost matching regions. `Q_0` is an unchanged witness query and `c` is a constant row. `↦` denotes a protected replacement and `↔` a symmetry. SC7 needs full expression subqueries, so among Cypher targets it runs only on Neo4j. Gremlin targets support all sixteen patterns; Gremlin SY3–SY8 are embedded in a `match()` step ([pattern embedding](#pattern-embedding)).

**Short-Circuit Patterns.** Short-circuit mutation starts from a query that already contains a short-circuit context. It replaces the low-cost computation in the position that can be skipped with a high-cost computation, while preserving the surrounding operators and the decisive condition. For example, `coalesce(NULL, 0, 1)` can become `coalesce(NULL, 0, COUNT {…})`. Both expressions return zero; only the last argument's potential evaluation cost changes. The non-null prefix is the semantic evidence that this argument can be skipped, regardless of whether the low-cost and high-cost arguments have the same value. Table 1 shows the source and mutated forms; the following cases explain their equivalence and the work that can be skipped.

*Boolean absorption (SC1, SC2).* Let T, F, and ⊥ denote true, false, and `NULL`, respectively, and let ∨ and ∧ denote Boolean disjunction and conjunction. For any Boolean-or-null operand value $e$, three-valued Boolean semantics gives

$$\mathrm{T} \lor e = \mathrm{T}, \qquad \mathrm{F} \land e = \mathrm{F}. \tag{1}$$

SC1 replaces $E_L$ with $E_H$ in $D_T$ `OR` $E_L$, while SC2 makes the same replacement under $D_F$ `AND`; the guards $D_T$ and $D_F$ are true and false for every eligible input binding, as a literal or as a test over the graph ([pattern embedding](#pattern-embedding)). The decisive operand and Boolean operator remain unchanged, and either replaced operand may return null. Evaluating the expensive operand cannot change the Boolean value or the enclosing filter's decision.

*Branch selection and non-null prefixes (SC3, SC4).* For a guard expression $C$ and a retained expression $V$, write $g$ and $v$ for their values. Then $\mathrm{select}(g, v, e)$ returns $v$ if $g = \mathrm{T}$ and a type-compatible alternative value $e$ otherwise. For a checked true guard and a non-null prefix value $v$, respectively,

$$\mathrm{select}(\mathrm{T}, v, e) = v, \qquad \mathrm{coalesce}(\bot, v, e) = v \quad (v \neq \bot). \tag{2}$$

SC3 replaces $E_L$ with $E_H$ in the unselected `CASE` branch, while retaining the selected value $V$; a later `WHEN` condition, which the true first condition makes unreachable, may be replaced in the same way. SC4 changes `coalesce(NULL, V, E_L)` to `coalesce(NULL, V, E_H)`, with $V$ constrained to be non-null. For $V = 0$, both forms return zero while the replaced argument can have a larger evaluation cost.

*Empty-result propagation (SC5, SC8).* For a row stream $X$ and a non-negative integer $n$, $\mathrm{limit}_n(X)$ retains at most $n$ rows, while $\sigma_\phi(X)$ retains rows satisfying predicate $\phi$. We write ∅ for an empty row stream and ⋈ for the inner join induced by mandatory matching. Then

$$\mathrm{limit}_0(X) = \sigma_{\mathrm{F}}(X) = \emptyset, \qquad \emptyset \bowtie X = \emptyset. \tag{3}$$

SC5 replaces a low-cost match with a high-cost match, retaining the constant projection $c$ and the zero limit. SC8 replaces an existing low-cost downstream match after a false filter on a mandatory matching fragment $A$. The zero output demand or empty input is unchanged, so neither replacement can contribute a result row and the high-cost traversal can be suppressed. These constructions exclude intervening operators, such as an ungrouped aggregate, that can produce a row from empty input.

*Bounded demand and existential witnesses (SC6, SC7).* SC6 and SC7 replace a low-cost branch $Q_L$ with $Q_H$; SC6 also retains a branch $Q_0$ that can satisfy a limit. Let $X_0$, $X_L$, and $X_H$ denote their row streams, and let ⊎ denote bag union. For SC6, every branch projects the same constant $c$, and $Q_0$ supplies at least $k > 0$ rows. Writing $\pi_c$ for this duplicate-preserving projection gives

$$\mathrm{limit}_k(\pi_c(X_0 \uplus X_H)) = \mathrm{limit}_k(\pi_c(X_0 \uplus X_L)) = \{\!\{c\}\!\}^k, \tag{4}$$

where the last term denotes $k$ copies of $c$. The mutation preserves the existing `UNION ALL` and limit; the result remains $k$ copies of $c$. The parentheses in the table denote grouping, with the limit applied to the combined output rather than to either branch separately. In the direct form of SC6, the $k$ witness rows belong to both regions instead of a separate branch. For SC7, $\mathrm{exists}(X)$ is true if $X$ contains at least one row and false otherwise. Because both $Q_L$ and $Q_H$ supply a witness row for every input binding, replacing $Q_L$ inside `EXISTS` also preserves the result:

$$\mathrm{exists}(X_H) = \mathrm{exists}(X_L) = \mathrm{T}, \tag{5}$$

The optional `NOT`, written `[NOT]` in the table, is retained. An existential subquery can stop after finding its first row. These patterns use streamable branches without blocking operators: evaluating $Q_0$ first can satisfy the limit without executing either $Q_L$ or its replacement $Q_H$, and the first row of $Q_H$ establishes existence without enumerating the others. Execution order is an optimization choice; diagnosis checks whether the engine misses this opportunity to avoid the expensive branch.

**Symmetry Patterns.** Symmetry mutation changes the representation or order of query components while preserving the complete result. Under the conditions in Table 1, equivalence supplies the semantic evidence for alternative execution choices, while differences in access costs and intermediate cardinalities expose optimization opportunities.

*Endpoint and path orientation (SY3, SY4).* For a matched path $p$, let $\rho(p)$ reverse its node and edge sequence, and let $o(p)$ denote the values observed by the enclosing query. Reversal preserves the observed matches when

$$\rho(\rho(p)) = p, \qquad o(\rho(p)) = o(p). \tag{6}$$

The resulting bijection preserves multiplicities. SY3 reverses the written orientation of a single relationship pattern while preserving its direction, and SY4 reverses a bounded undirected path. These patterns target traversal from a more selective endpoint or through a smaller neighborhood to reduce intermediate matches.

*Equivalent expressions (SY1, SY2).* For Boolean-or-null values $a$ and $b$, commutativity holds under three-valued semantics:

$$a \land b = b \land a, \qquad a \lor b = b \lor a. \tag{7}$$

SY1 and SY2 exchange two unchanged operands of `AND` and `OR`; these identities test whether equivalent forms retain applicable index access and effective predicate-evaluation orders.

*Matching and branch order (SY5–SY8).* For mandatory matching results $X$ and $Y$,

$$X \bowtie Y = Y \bowtie X. \tag{8}$$

SY5 exchanges two independent components, SY6 two branches that share only an anchor, and SY7 the two stages of a chain. Optional-branch exchange (SY8) additionally preserves each branch's null extension under the independence conditions in the table. For nested-loop matching, the outer cardinality determines how often the inner component executes, motivating orders that reduce repeated work or intermediate results. Execution plans and work counters establish whether the targeted opportunity was missed.

**Skeleton Construction.** After selecting a pattern, GraphSymPerf encodes its source form, mutation, and applicability conditions in a parameterized query skeleton. First, it specifies the source structure and the eligible transformation site, such as a short-circuited Boolean operand, an unselected branch, or independent `MATCH` components. Second, it abstracts labels, relationship types, properties, predicates, and subqueries into placeholders, recording each placeholder's syntactic category, type, and variable dependencies. Third, it assigns cost roles and semantic constraints to executable placeholders, including low-cost and high-cost alternatives for the short-circuited component. A low-cost component may be a constant or selective lookup, whereas a high-cost component may require expansion, path enumeration, or aggregation over a large neighborhood. These roles specify the potential work to be supplied by graph construction. For reordering patterns, output cardinalities are also recorded because one component's rows can multiply the evaluation cost of another. For example, the SC4 skeleton retains `coalesce(NULL, V, E)`, where $E$ is an expression placeholder, constrains $V$ to be non-null, and assigns $E_L$ and $E_H$ as its alternatives. The source query uses $E_L$; mutation replaces only that argument with $E_H$. The non-null constraint establishes Equation 2; the high-cost role makes an unnecessary evaluation observable. The subsequent graph-generation step supplies data satisfying these constraints.

## Query Generation and Mutation

A skeleton identifies an optimization opportunity, but its cost and its equivalence depend on the query into which it is embedded. Graph queries reach data by traversing from variables that the enclosing query has bound, as in the correlated subquery `COUNT { MATCH (v2)-->()-->() }`; a larger graph makes such an operand expensive only if the added data is reachable from the nodes to which the anchor `v2` binds. The low-cost and high-cost regions coexist in $G$. Separate labels and relationship types direct each operand to its region, while the surrounding host supplies the input bindings. Algorithm 1 therefore generates a random source query together with its scope table ([seed query generation](#seed-query-generation)), embeds the skeleton of a pattern $p$ at a position and from an anchor chosen through the scope table ([pattern embedding](#pattern-embedding)), and applies the mutation to a copy ([naming and mutation](#naming-and-mutation)). Every name and constant is final when it is drawn, so no later step fills in the query. Besides the pair $(Q, Q')$, Algorithm 1 returns the graph requirements `req`, from which [graph state generation](#graph-state-generation) builds the graph state.

**Running example.** The seed-query example below traces `GenerateAST` and its scope table. The SC1 and SY6 examples then use that query to show pattern embedding and graph construction.

```text
Algorithm 1: Query Generation and Mutation
Input:  capability set B, limits Ω, mutation pattern p
Output: query pair (Q, Q′) and requirements req, or ⊥
 1: // Step 1: Random AST generation
 2: T ← GenerateAST(∅)              ▷ names final when drawn
 3: sites ← ScopeAnalysis(T)        ▷ scope table per node
 4: // Step 2: Pattern embedding
 5: C ← {s ∈ sites | p is applicable at s}
 6: if C = ∅ then return ⊥
 7: s ← a random element of C
 8: a ← SelectAnchor(scope table at s)
 9: if p is a short-circuit pattern then
10:     (E_L, E_H) ← WorkOperands(p, a); D ← Guard(p, a)
11:     F ← p.Form(D, E_L); edit ← replace E_L by E_H
12: else                        ▷ symmetry: no determinant
13:     F ← p.Form(T[s], a); edit ← swap components of F
14: end if
15: T[s] ← F; req ← p.Requirements(a, F)
16: // Step 3: Query mutation
17: Q ← T; Q′ ← edit applied to a copy of Q
18: if not Consistent(Q, Q′) then return ⊥
19: return (Q, Q′, req)

20: function GenerateAST(S)         ▷ S: scope table
21:     choose the form and the clause count k within Ω
22:     for i ← 1 to k do
23:         c ← a clause kind that B and Ω allow
24:         if c = CALL then
25:             body ← GenerateAST(child table of S)
26:             add the outputs of body to S.local
27:         else
28:             expand c with pooled names and GenExpr
29:             update S by the rule of c
30:         end if
31:     end for
32:     return the k clauses and a RETURN
33: function GenExpr(κ, S)
34:     if depth or budget exhausted, or at random then
35:         return a visible κ entry, x.k_κ or κ constant
36:     return a κ production with children from GenExpr
```

### Seed Query Generation

The seed query supplies the context in which a pattern is embedded: the clauses around the mutation point, the variables in scope, and the outputs that observe the mutated value. GraphSymPerf generates it from the [compact grammar](#compact-grammar) below.

#### Compact grammar

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

A node pattern either introduces a fresh variable, which carries at least one label, or refers to an already bound variable, written `(id)`; every relationship pattern has a type. The namespace separation of [naming and mutation](#naming-and-mutation) relies on these two restrictions.

**Generation.** `GenerateAST` (Algorithm 1, lines 2 and 20–36) expands the grammar top-down from $Q$. At each step, it samples only among productions that are well typed, reference only variables in scope, and belong to the capability set $B$ of the target DBMS. $B$ records the dialect differences that affect generation, such as whether `CALL` may appear inside expression subqueries, whether a `WITH` clause keeps imported variables, and whether subqueries are written as full queries, as `MATCH` bodies or as pattern comprehensions. Generation limits $\Omega$ bound the expansion budget, the number of clauses and the nesting depth; once the budget is exhausted, only terminating productions remain. To keep results deterministic, the generator precedes every `SKIP` or `LIMIT` with an `ORDER BY` over all scalar output columns and applies aggregates only to constants.

**Names and constants.** Terminals are final when the generator draws them. They come from fixed, finite *pools*: labels (`ℓ`) from `Random_label_0`–`Random_label_3`, relationship types (`r`) from `Random_relationship_0`–`Random_relationship_2`, property keys (`k`) from one key per value type (`property_int`, `property_float`, `property_bool` and `property_string`), and constants (`c`) from small typed domains that the background graph also uses for its property values ([graph state generation](#graph-state-generation)): the integers −2 to 4, the floats 0.5, 1.5 and 2.5, both Booleans, and the strings `a`, `b` and `c`. Each name records its kind (label, relationship type or typed key), so later steps can collect the labels, types and keys that the query uses. Instantiation is therefore part of generation rather than a separate step: when `GenerateAST` returns, every variable name, label, relationship type, property key and constant of the seed query is final, and the query can be rendered and executed as is. Later steps add the pattern's fragment with names from a separate namespace ([naming and mutation](#naming-and-mutation)), but they never fill in or change a seed name or constant.

**Scope table.** While it expands the grammar, the generator maintains a *scope table* that lists the variables a production may reference. Each entry describes one query variable with the fields of Table 2.

**Table 2: Fields of a scope-table entry.**

| Field | Values | Meaning |
|---|---|---|
| `name` | `v1`, `v2`, … | The variable's name, numbered in creation order and final when created. |
| `kind` | `node`, `relationship`, `path`, `int`, `float`, `bool`, `string`, `list_T` | The value type. A production references only entries of the kind it needs; a property access `x.k` needs a node or relationship. |
| `labels` | a set of seed labels, possibly empty | The labels that every binding of the variable carries: those written in the node pattern that declares it, or those of the source of a direct alias such as `WITH x AS y` or `RETURN x AS y`. It is empty for relationships, paths and scalars. |
| `nullable` | `true`, `false` | Whether the variable can be null because it is declared by an `OPTIONAL MATCH` or aliases such a variable. A non-null node variable is bound to an existing node in every row. |
| `part` | `imported`, `local` | `imported` entries are visible because an enclosing `CALL` imports them or because an expression subquery sees its enclosing scope; `local` entries are declared in the current query part. |

The variables visible at a point are the imported and local entries together. Each clause updates the table when it is complete:

- `MATCH` adds every fresh node, relationship and path variable to `local`, with the labels of its node pattern; `nullable` is true for `OPTIONAL MATCH`. A reference `(x)` to a bound variable adds nothing.
- `WITH` replaces `local` with its projected aliases. A direct alias copies `kind`, `labels` and `nullable` from its source; any other expression gives its kind and no labels. The `imported` entries remain visible if $B$ says that the DBMS keeps `CALL` imports across `WITH` (Neo4j does; ArcadeDB does not, so they are dropped).
- `UNWIND` adds its variable to `local` with the element kind of the list.
- `CALL (x, …) { Q }` generates $Q$ against a new table whose `imported` part holds exactly `x, …`. When $Q$ is complete, the columns that $Q$ returns are added to the caller's `local` part; nothing else leaves the `CALL`.
- An expression subquery (`EXISTS`, `COUNT`, `COLLECT` or a pattern comprehension) generates its body against a child table whose `imported` part is everything visible; the variables it declares do not leave the subquery.
- `RETURN` ends the query part. Its aliases are the output columns; they are added to `local` by the same rules as the aliases of `WITH`, so a direct alias copies the fields of its source. For a `CALL` body, the caller receives them instead (see `CALL`).

**Generation loop** (lines 20–36). `GenerateAST` is a loop over clauses. The generator first draws the top-level clause sequence: the form of the query, a single part or a `UNION` of two parts with the same output columns, and the number $k$ of clauses, which remain uninstantiated. Each of the $k$ passes picks the next clause among the productions that $B$ and the depth limit allow, expands the clause's items from left to right, and updates the scope table once the clause is complete. Every item is created for a required kind against the table as it stands before the clause; the only exception is the `WHERE` of a `MATCH`, which also sees the variables that the `MATCH` declares. At a leaf, the generator picks a visible entry of the required kind, a property of a visible node or relationship under the key of that kind, or a constant of that kind from the pools. A `CALL` runs the same loop on its body against a child table, and the body's output columns enter the caller's table. After the $k$ passes, the `RETURN` projects the output columns; the seed query is then complete and instantiated.

The following six steps generate the seed query and update its scope table. Step 1 draws the top-level clause sequence, a single query part with $k = 2$ uninstantiated clauses followed by a `RETURN`. Steps 2–6 instantiate these clauses in order and produce lines 1–5 of the query below; the two clauses become a `MATCH` and a `CALL`. In step 2 (line 1), the table is empty, so the `MATCH` declares a fresh node `v1` and draws the labels `U0:U1` from the label pool. In step 3 (line 2), the `CALL` reads the visible variables, imports a subset of them and takes `v1`; its body starts with `v1` as its only, imported, entry. In step 4 (line 3), the `WITH` aliases `v1` as `v2`, which copies `node`, `U0:U1` and non-null, and creates a float expression whose leaf is the property of `v1` under the float key `score`, named `v3`; because Neo4j keeps `CALL` imports, `v1` remains visible next to `v2` and `v3`. In step 5 (line 4), the inner `RETURN` creates a Boolean expression whose leaf is the property of `v2` under the Boolean key `flag`, named `v4`; when the `CALL` ends, only `v4` enters the outer table. In step 6 (line 5), the outer `RETURN` reads `v1` and `v4` and projects them as the output columns `v5` and `v6`, which enter the table as direct aliases of `v1` and `v4`. The instantiated seed query is

```cypher
MATCH (v1:U0:U1)                   // 1
CALL (v1) {                        // 2
  WITH v1 AS v2, v1.score AS v3    // 3
  RETURN v2.flag AS v4             // 4
}
RETURN v1 AS v5, v4 AS v6          // 5
```

**Scope analysis** (line 3). After generation, a scope analysis walks the finished AST with the same rules and records a *site* for every AST node $s$: the position of $s$, the node itself, and a copy of the scope table visible at $s$. For a `MATCH` clause, the recorded table is its input, that is, the table before the clause. The scope analysis also rejects a query that references an invisible variable or mixes kinds. The sites are the only input that [pattern embedding](#pattern-embedding) reads besides the AST. The seed query has two sites used below. At the first `MATCH`, the table is the empty table of step 1. At the projection `v2.flag`, it is the `CALL body` table after step 4, which holds the imported `v1` and the local `v2` and `v3`.

The seed query does not depend on the pattern, so the same seed query can host different patterns.

**Gremlin.** For Gremlin, GraphSymPerf generates the linear subset of the [grammar](#compact-grammar), which excludes `UNWIND`, `CALL` and pagination, and renders it as traversal steps (Table 3). Its scope table has a single current entry, the object that the traverser carries, with a `kind` and `labels` (set by `hasLabel`), plus the entries that earlier `as(·)` steps have named and `select(·)` can read back; a reduction such as `count()` clears the named entries. We present the approach in terms of Cypher; the Gremlin adaptation differs only in seed generation and embedding.

**Table 3: Rendering grammar constructs in Gremlin.**

| Construct | Gremlin |
|---|---|
| `MATCH (x:ℓ)` | `g.V().hasLabel(ℓ)` |
| `MATCH (x)-[:r]->(y:ℓ)` | `.out(r).hasLabel(ℓ)` |
| `WHERE` | `.filter(·)` |
| `WITH x AS y` | `.as(y)`, read back with `.select(y)` |
| `CASE`, `coalesce` | `.choose(·)`, `.coalesce(·)` |
| `COUNT {…}` | `.count()` |

### Pattern Embedding

**Slots.** Each skeleton of Table 1 accepts one kind of syntactic slot: Boolean expressions host SC1, SC2, SC7, SY1 and SY2; scalar expressions host SC3 and SC4; row sources with scalar output host SC5, SC6 and SC8; single-edge path patterns host SY3 and SY4; and mandatory `MATCH` clauses without `WHERE` host SY5–SY8.

**Determinants.** Every short-circuit form contains an unchanged part that fixes its result before the protected operand matters.

**Definition 1 (Determinant).** Let $F$ be a short-circuit form with protected operand $E$. An unchanged part $D$ of $F$ is a *determinant* of $F$ if, for every input binding $\eta$ that reaches the mutation point, the value of $D$ under $\eta$ fixes the value of $F$ under $\eta$ (for a row source, its output rows), whatever $E$ evaluates to.

For SC1 the determinant is a true `OR` operand $D_T$ and for SC2 a false `AND` operand $D_F$; for SC3 it is the first `WHEN` condition, which is true; for SC4 the non-null prefix $V$ of `coalesce`; for SC5 the zero demand `LIMIT 0`; for SC6 the $k$ witness rows before `LIMIT k`; for SC7 the first row of the `EXISTS` subquery; and for SC8 the empty mandatory input. A determinant either reads no data, such as a literal, a constant comparison, `LIMIT 0` or `WHERE FALSE`, or it reads the graph from the anchor, such as an `EXISTS` or `COUNT` test over a witness region. In the second case, $D$ is decisive only on a graph that contains its *witness*, the data that makes $D$ take its decisive value for a binding; [graph state generation](#graph-state-generation) therefore writes the witness on every candidate root.

Symmetry patterns have no determinant: no part of a symmetric form fixes its result. The form and its edit are equal on every graph because the construct itself is symmetric, so their equivalence needs no data. For example, `AND` and `OR` are commutative in three-valued logic; two mandatory `MATCH` clauses that do not use each other's variables form a bag join on their shared variables, which is commutative; and reversing a path pattern yields the same bindings of its variables. The components differ only in cost: one is *selective*, with few matches on $G$, and the other is *expansive*, with matches in proportion to $N$, and the written order may decide how often the planner evaluates the expansive one. The guard-swap variant of SY1 and SY2 places a constant or witnessed operand next to a high-cost operand and swaps them. That operand fixes the value of `AND` or `OR`, but the swap is equivalent without it, so for SY1 and SY2 it only lets one of the two orders skip the high-cost operand.

Algorithm 1 accesses a pattern $p$ only through its skeleton ([patterns and skeleton construction](#patterns-and-skeleton-construction)): its slot, its applicability conditions from Table 1, a function `Form` that builds the fragment replacing the slot, and a function `Requirements` that derives the graph requirements; short-circuit patterns additionally provide candidate pools for their operands (`WorkOperands`) and determinants (`Guard`). We say that $p$ is *applicable* at an AST node $s$ if $s$ fits the slot of $p$, $B$ supports every construct that $p$ introduces, and the context of $s$ satisfies the applicability conditions of $p$. For example, SY5–SY8 require that neither component uses variables introduced only by the other, and SY3 and SY4 require that the enclosing query does not observe the order of the path.

**Mutation point selection** (Algorithm 1, lines 5–7). GraphSymPerf reads the site of every AST node ([seed query generation](#seed-query-generation)), keeps the nodes at which $p$ is applicable and samples one of them. The seed query's projection `v2.flag` is a Boolean slot for SC1 with `v1`, `v2` and `v3` in scope; `v1` remains visible after the `WITH` because the target DBMS keeps the imports of `CALL`. Although `v2` never appears in a labelled `MATCH`, it inherits `U0:U1` and non-nullability from `v1` through the alias and can therefore start a correlated path; `v3` is a float and cannot. A choice by name could pick `v3`, and a check limited to the labels of the current clause would miss `v2`. The first `MATCH` is a slot for SY6: its input scope is empty, but the clause itself declares `v1`, which the later `CALL (v1)` uses, so SY6 reuses `v1` as the shared root of its components.

**Anchor selection** (line 8). An anchor candidate is a visible entry of the scope table with `kind` = `node`, a non-empty `labels` set and `nullable` = false; at the `v2.flag` projection, these are `v1` and `v2`. The three fields serve three purposes: the anchor must be a node from which a path can start, its labels later identify the nodes it can bind to ([graph state generation](#graph-state-generation)), and a non-null anchor is bound to an existing node in every row. `SelectAnchor` chooses between a *correlated* fragment, which reuses a visible non-null node variable with known labels as its anchor so that its cost depends on the bindings produced by the seed query, and an *uncorrelated* fragment, which starts from a fresh root and exercises subqueries that do not depend on outer bindings. When candidates exist, it chooses a correlated fragment with probability $p_c$; otherwise it uses a fresh root. Clause-level symmetry patterns such as SY6 always reuse the variables that the replaced clause declares.

**Short-circuit embedding** (lines 9–11). GraphSymPerf samples a work candidate, namely a one-hop count, a two-hop count, a non-matching `EXISTS` or a row source, and instantiates it twice from the anchor $a$: once on a low-cost region and once on a high-cost region. The resulting operands $E_L$ and $E_H$ have the same shape, but their relationship types and end labels belong to disjoint regions. The determinant $D$ must have the same value for every binding of $a$; it is a literal, a constant comparison, a count or existence test over a separate witness region, or a property of the anchor wrapped in `coalesce`. The embedded form $F$ uses $E_L$, and the edit replaces $E_L$ with $E_H$. For instance, SC1 rewrites a Boolean expression and SC4 rewrites a scalar expression:

$$BE \;\rightarrow\; D_T \;\texttt{OR}\; E_L, \qquad E \;\rightarrow\; \texttt{coalesce}(\texttt{NULL},\, V,\, E_L),$$

$$E_X \;::=\; \texttt{COUNT}\,\{\texttt{MATCH}\,(a)\text{-}[{:}r_X]\text{->}()\text{-}[{:}r'_X]\text{->}(t)\;\texttt{WHERE}\;t.\texttt{keep}\} > 0 \;\mid\; \cdots, \qquad X \in \{L, H\},$$

where $D_T$ is a determinant that is always true and $V$ is an unchanged non-null value. The SC1 example uses the anchor `v2`, an existence test as its determinant and a two-hop count as its work operand. The query fragments omit the labels of the fresh nodes `w`, `t` and the unnamed middle nodes; each of them carries a label from the pattern namespace ([naming and mutation](#naming-and-mutation)). The graph-construction example names the middle and end nodes of the two regions $m_L$, $t_L$ and $m_H$, $t_H$:

```text
D_T = EXISTS { MATCH (v2)-[:W]->(w) WHERE w.keep }
E_X = COUNT  { MATCH (v2)-[:X1]->()-[:X2]->(t) WHERE t.keep } > 0      X ∈ {L, H}
F   = D_T OR E_L                                                      replaces v2.flag
```

**Symmetry embedding** (lines 12–13). The form reuses the components at the mutation point or extends them with fresh ones, and the edit reorders two components without changing either. In the SY6 example, the pattern replaces the first `MATCH` with two mandatory components that share only `v1`. The recipe also chooses which component is selective, here $A$ with a single `R` edge, and which is expansive, here $B$ with $N$ `S` edges; the property `{sel: 1}`, short for the pattern key `p_anchor`, restricts the root to a single node:

```text
A = MATCH (v1:U0:U1 {sel: 1})-[:R]->(y)
B = MATCH (v1)-[:S]->(z)
F = A B,  edit = swap A and B
```

**Graph requirements** (line 15). Embedding also records what the graph state must contain. The requirements `req` consist of three parts:

- the *anchor*, the scope-table entry of $a$; its `labels` identify the candidate roots;
- the *equivalence paths*: the witnesses of the determinant, which every candidate root needs so that $D$ keeps its decisive value. They are empty if the determinant reads no data, and always empty for symmetry patterns, which have no determinant;
- the *cost paths*: the paths of the low- and high-cost operands, or of the selective and expansive components, with a layout that states whether one root or every root carries them. Each such root gets a *fixed part* built once, which holds the low-cost side ($E_L$, or the selective component), and *units* repeated in proportion to $N$, each one high-cost match ($E_H$, or one match of the expansive component).

All random choices of the pattern are made here and recorded in `req`: the anchor, the kind of determinant, the work candidate and, for a symmetry pattern, which component is selective. Given `req`, the pattern part of the graph is determined. The graph-construction example below expands these requirements, and Table 5 ([graph state generation](#graph-state-generation)) lists the requirements of all patterns.

**Gremlin.** GraphSymPerf supports all sixteen patterns for Gremlin. Short-circuit patterns and SY1/SY2 are embedded at `filter`, `map` or final traversal steps. SY3–SY8 insert a declarative `match()` step with two patterns after the root `V().hasLabel(·)`, because Gremlin otherwise executes steps in the written order. SY5–SY8 exchange the two patterns; SY8 replaces null extension by `coalesce(·, constant(sentinel))`. For SY3 and SY4 the first pattern binds a target and the second is a `where()` filter over one edge or a bounded undirected walk, which the edit rewrites from the target back to the root. The filter uses `where()` rather than an end label because TinkerPop infers the start label of `match()` from pattern order, and a reversed end-label pattern listed first silently binds the wrong object. Because a traverser carries a single current object, the anchor is either that object or a node named by an earlier `as(·)` step and reached through `select(·)`, so every Gremlin fragment is correlated. The work operands are one-hop traversals such as `out(r_X).has('keep', true)`, and the short-circuit edit replaces the relationship type $r_L$ with $r_H$.

### Naming and Mutation

**Names and constants** (Algorithm 1, lines 2 and 9–15). Every name is final when it is created, so no binding step follows embedding. The seed names and constants are fixed during seed generation ([seed query generation](#seed-query-generation)). Embedding names everything the pattern adds, including the edit and the graph requirements, in a separate `Pattern_` namespace. Each name records its kind (label, relationship type, or typed property key), which the background graph and the schema declarations of [graph state generation](#graph-state-generation) use. Table 4 shows part of the names for SC1 and where each name is used.

**Table 4: Names used by the SC1 example.**

| Abbreviation | Owner | Name | $Q$ | $Q'$ | $G$ |
|---|---|---|:-:|:-:|:-:|
| `U0`, `U1` | seed | `Random_label_0`, `Random_label_1` | ✓ | ✓ | ✓ |
| `flag`, `score` | seed | `property_bool`, `property_float` | ✓ | ✓ | ✓ |
| `W` | pattern | `Pattern_p_witness_edge` | ✓ | ✓ | ✓ |
| `L1`, `L2` | pattern | `Pattern_p_low_first_edge`, `Pattern_p_low_edge` | ✓ | | ✓ |
| `H1`, `H2` | pattern | `Pattern_p_high_first_edge`, `Pattern_p_high_edge` | | ✓ | ✓ |
| labels of `w`, $m_L$, $t_L$, … | pattern | `Pattern_p_witness`, `Pattern_p_low_middle`, `Pattern_p_low`, … | ✓ | ✓ | ✓ |

Naming at creation provides two guarantees on which the rest of the approach relies.

*Shared names.* $Q$, $Q'$ and $G$ use the same names, including names that only some of them mention. In Table 4, the high-cost types `H1` and `H2` never occur in $Q$, yet [graph state generation](#graph-state-generation) must build $N$ paths of exactly these types for $Q'$ to traverse; conversely, `L1` and `L2` occur in $Q$ but not in $Q'$. Embedding creates the edit and the graph requirements together with $F$, so $Q'$ and $G$ refer to the same names, and the mutation itself draws no names or constants.

*Namespace separation.* Seed labels and relationship types are disjoint from pattern labels and relationship types. In the running example, the high-cost end nodes therefore do not become additional matches for `MATCH (v1:U0:U1)`. Property keys such as `keep` may occur in both regions. The labels and relationship types on a traversal determine which region its predicates inspect.

**Mutation** (lines 17–18). The embedded AST is the base query $Q$; GraphSymPerf applies the edit to a copy of $Q$ to obtain $Q'$; the short-circuit mutation replaces $E_L$ with $E_H$, and the symmetry mutation reorders the two components of $F$. A static check then requires both queries to satisfy the scope rules of [seed query generation](#seed-query-generation), to expose the same output columns, and to differ in text. Whether their results are equal is decided by execution ([result and performance checking](#result-and-performance-checking)). The SC1 and SY6 examples show the corresponding local forms.

## Graph State Generation

A skeleton identifies an optimization opportunity, but it does not determine how much work is lost when that opportunity is missed. With little data or similar access costs, inefficient execution may produce only a small timing difference. Graph state generation therefore constructs data that amplifies the intended cost contrast while keeping $Q$ and $Q'$ equivalent. The equivalence itself may depend on the data: a guard that reads the graph, such as an `EXISTS` test, fixes the result only if it holds for *every* binding that reaches the mutation point.

Algorithm 2 builds the graph state $G$ from `req`. Each of its four steps is decided by one source, as described in the graph-construction example below: randomness decides the background graph, to which step 1 adds one match of the seed query (the host witness); the anchor's entry in the scope table decides the candidate and workload roots (step 2); the determinant decides the equivalence paths (step 3); and the operands or components decide the cost paths (step 4). Only step 1 is random; given `req`, the rest is deterministic. Steps 3 and 4 answer two separate questions. Which paths does the equivalence of $Q$ and $Q'$ depend on, and on which roots must they exist? Which paths carry the cost, and on which roots should it concentrate?

```text
Algorithm 2: Graph State Generation
Input:  query pair (Q, Q′), requirements req, scale N
Output: graph state G on which Q and Q′ agree, or ⊥
 1: // Step 1: Background graph
 2: G ← RandomGraph(seed names) ∪ HostWitness(Q)
 3: // Step 2: Candidate and workload roots
 4: roots ← nodes with the labels of req.anchor
 5: if roots = ∅ then roots ← [a new such node]
 6: if req.layout = PerRoot then    ▷ SC1–SC8, SY1, SY2
 7:     work ← roots
 8: else                            ▷ SY3–SY8
 9:     work ← [first node of roots]
10: end if
11: if req.unitPerRoot and N < |work| then return ⊥
12: // Step 3: Equivalence paths
13: for each root r in roots do
14:     for each path w in req.equivalence do
15:         AddPath(G, r, w)        ▷ D decisive at r
16:     end for
17: end for
18: // Step 4: Cost paths
19: (q_1, …, q_k) ← Split(N, |work|)  ▷ near-equal shares
20: for i ← 1 to k do
21:     h ← AddPath(G, work[i], req.fixed)  ▷ low-cost side
22:     for j ← 1 to q_i do
23:         AddPath(G, h, req.unit)   ▷ one high-cost match
24:     end for
25: end for
26: // Step 5: Schema registration
27: register the names of Q, Q′ and G in the schema
28: return G

29: function AddPath(G, u, f)
30:     add the nodes, edges and properties of f at u
31:     return the node of f that later fragments extend
32: end function
```

**Background graph** (lines 1–2). `RandomGraph` is the only random step of Algorithm 2. It creates $n_0$ nodes, gives each one or two labels drawn from the seed labels, and sets the marker properties `keep` and `value` and, with high probability, each seed key to a value drawn from the domain of its type ([seed query generation](#seed-query-generation)). It then adds $m_0$ edges with uniformly drawn endpoints and seed relationship types. It does not solve the predicates of the seed query; it only provides a context that is independent of the pattern. Because it uses seed names only, no background node carries a pattern label ([naming and mutation](#naming-and-mutation)). The random choices of the pattern itself, such as the anchor, the kind of determinant and the selective component, were made during embedding ([pattern embedding](#pattern-embedding)) and are recorded in `req`, so for a given seed the rest of Algorithm 2 is deterministic.

**Host witness** (line 2). Random edges rarely complete a multi-hop host pattern, so without help many pairs return no rows and the workload sits on roots that the seed query never reaches. `HostWitness` therefore adds one instance of the seed query's mandatory matching: for Cypher, every non-optional `MATCH` path, with its labels, relationship types, shortest allowed length and literal property filters (inline maps and `=` conjuncts in `WHERE`); for Gremlin, the root traversal's `out`/`in`/`both` + `hasLabel` steps and simple `has` filters, continuing past the pattern's own steps. A node variable reuses the first existing node with its labels, which symmetry recipes also choose as their root. Pattern-owned names (`Pattern_` types, `p_` keys) are left to the recipe. Because `HostWitness` runs before the candidate roots are collected, a planted node that carries the anchor's labels is a candidate like any other and receives the equivalence and cost paths of lines 12–25, so the witnesses still cover every binding of the anchor. After line 25, the candidate roots of a short-circuit anchor also receive the equality filters that the seed query places on the anchor itself.

**From the scope table to the roots** (lines 3–11). The anchor's entry in the scope table determines where the pattern data starts. Its `kind` is `node` and its `nullable` is false, so every binding of the anchor at the mutation point is an existing node; its `labels` are carried by every such node. The candidate roots are therefore the nodes that carry all of these labels, in creation order (line 4), and every binding of the anchor is a candidate root, because the anchor's labels come from a `MATCH` pattern (a `hasLabel` step in Gremlin) or from an alias of a matched variable. If there are none, a new root is created. Filtering by label alone over-approximates the inputs, since other predicates of the seed query may stop some candidates from reaching the mutation point. The workload roots are all candidates for short-circuit patterns, SY1 and SY2, and only the first candidate for the other symmetry patterns. SY8 is the one exception: it adds three roots next to the first, so that the four roots cover the cases in which both, one or neither of its optional branches match (Table 5). Line 11 rejects a case whose pattern needs at least one unit on every workload root but whose $N$ is smaller than the number of such roots; for example, SC7 needs a high-cost match on every root so that `EXISTS` keeps its value.

**Which paths go where.** Algorithm 2 decides the placement of each path that `req` mentions by one question: can the path change the result of $Q$ or $Q'$? Only the witness of a determinant can. If it is missing for one binding, $D$ loses its decisive value for that binding, the protected operand decides the result, and $Q$ and $Q'$ may differ. The witness is therefore an *equivalence path*, and it goes on every candidate root, because the anchor may bind to any of them. The paths of $E_L$ and $E_H$ cannot change the result: $D$ already fixes it, so their values are irrelevant, and they only determine how much work an evaluation that ignores $D$ performs. They are *cost paths* and go on the workload roots; the no-match `EXISTS` candidate even writes end nodes that fail its predicate. Symmetry patterns have no determinant, so none of their paths can change the result, and all of them are cost paths. They go on a single root, where the selective component gets a fixed part and the expansive component gets the $N$ units; every other candidate contributes the same rows to both forms. The guard-swap variant of SY1 and SY2 writes its operand's witness on every root, but as a cost path: it lets one order skip the high-cost operand for every binding.

**Equivalence paths** (lines 12–17). For each candidate root, Algorithm 2 adds every path of `req.equivalence` (Table 5). A determinant that reads no data, such as a literal or `LIMIT 0`, needs none; one that reads an anchor property, such as `coalesce(a.p_guard, true)`, needs that property on every root instead of a path.

**Cost paths** (lines 18–25, 29–32). `Split` divides $N$ into near-equal integer shares over the workload roots. For each workload root, line 21 builds the fixed part once, and lines 22–24 attach $q_i$ units to the node `h` that the fixed part returns. Only the units count towards $N$. Because `h` can be an inner node, a two-hop high-cost region creates its middle node once, and each further match adds a single edge from it. A fragment may also consist of properties only, such as `sel = 1` in SY6.

**Schema registration** (lines 26–27). Finally, Algorithm 2 declares the labels, relationship types and property keys of both queries and of $G$ in the DBMS schema, so that empty regions are declared as well.

**Running example.** Algorithm 2 constructs the SC1 and SY6 graph states as follows. *Step 1.* $G_0$ consists of three unconnected nodes: `n0` and `n1` labelled `:U0:U1`, with `flag` set to `false` and `true`, and `n2` labelled `:U2`. The host witness of `MATCH (v1:U0:U1)` reuses `n0` and adds nothing. *Step 2.* Both anchors, `v2` for SC1 and `v1` for SY6, are non-null nodes with labels `U0:U1` in the scope table, so `n0` and `n1` are the candidate roots and `n2` is not. *Step 3.* The determinant $D_T$ of SC1 is an `EXISTS` test over `W`. `v2` can bind to `n0` in one call and to `n1` in another, and a witness on `n0` cannot make the test true for `n1`, so both roots receive a `W` edge. SY6 has no determinant and receives no equivalence path. *Step 4.* SC1 places its cost paths on both roots: each root gets the fixed part, the `L1`·`L2` path and the `H1` edge to $m_H$, and $N/2$ units, each an `H2` edge from $m_H$. SY6 places its cost paths on `n0` only: the fixed part sets `sel = 1` and adds one `R` edge for the selective component $A$, and the $N$ units are `S` edges for the expansive component $B$. Both pairs are equivalent on $G$: $Q(G) = Q'(G) = \{(n0, \text{true}), (n1, \text{true})\}$ for SC1, and $N$ copies of $(n0, \text{false})$ for SY6. The local work differs as follows: if evaluated, $E_L$ matches 2 paths whereas $E_H$ matches $N$, and the first `MATCH` of SY6 yields 1 row in $Q$ but $N$ rows in $Q'$.

Table 5 summarizes the requirements of all patterns. Short-circuit patterns spread the workload over every candidate root, because the determinant must be decisive for every binding; symmetry patterns concentrate it on one root, because their equivalence needs no data.

**Table 5: Determinants and graph requirements of the mutation patterns.**

| Patterns | Determinant | Equivalence paths on every candidate root | Workload roots | Fixed part per workload root | Unit |
|---|---|---|---|---|---|
| SC1, SC2 | a true `OR` or false `AND` operand | one witness match if $D$ is an `EXISTS` or `COUNT` test | all | one low-cost match and the shared prefix of the high-cost matches | one high-cost match |
| SC3 | the first `WHEN` condition, which is true | as for SC1 | all | as for SC1 | one high-cost match |
| SC4 | the non-null `coalesce` prefix $V$ | one witness match if $V$ is a `COUNT` | all | as for SC1 | one high-cost match |
| SC5 | `LIMIT 0` | none | all | one low-cost row | one high-cost row |
| SC6 | $k$ witness rows before `LIMIT k` | $k$ witness rows | all | one low-cost row | one high-cost row |
| SC7 | the first row of `EXISTS` | one low-cost and at least one high-cost row (line 11) | all | one low-cost row | one high-cost row |
| SC8 | the empty mandatory input `WHERE FALSE` | none | all | one low-cost row | one high-cost row |
| SY1, SY2 | none | none; the guard-swap operand's witness is a cost path on every root | all | as for SC1 | one high-cost match |
| SY3 | none | none | first | the endpoint constraint on the root | one edge from the root, or from a node that fails the endpoint constraint |
| SY4 | none | none | first | one valid bounded path | one bounded path that fails the node predicate at a shared hub or at its end |
| SY5 | none | none | first | a selective component matching at most one node | one node of the expansive component |
| SY6 | none | none | first | the selective arm (one edge) | one edge of the expansive arm |
| SY7 | none | none | first | one complete chain | one right-hand edge that does not join the root |
| SY8 | none | none | four roots covering both, either and neither branch | one selective-branch edge on each root where it matches | one expansive-branch edge on a root where it matches |

## Result and Performance Checking

GraphSymPerf loads $G$ once per test case. The query pair and its measurements use unchanged data and indexes.

**Result check.** GraphSymPerf executes $Q$ and $Q'$ and compares their outputs as typed result multisets, including column names and row multiplicities. Normalization preserves scalar types, graph-entity identities, and order within returned lists and paths. A mismatch is recorded as `result_mismatch` for review of the database behavior and case construction. Every later execution is checked against the same reference result.

**Timing.** GraphSymPerf measures each pair in *discovery* and, when discovery qualifies, *confirmation*. Each phase has $w$ warm-up rounds and $n$ timed rounds. A round executes each query once; consecutive rounds alternate between $QQ'$ and $Q'Q$ to balance order effects. Let $t$ and $t'$ be the median times in a phase, and call the query with the larger median the slower one. The phase qualifies if

$$\frac{\max(t, t')}{\min(t, t')} \ge \rho, \qquad \operatorname{median}_i\,(\text{slower}_i - \text{faster}_i) \ge \delta, \qquad \frac{\lvert\{i : \text{slower}_i > \text{faster}_i\}\rvert}{n} \ge \kappa.$$

The thresholds require a relative gap of at least $\rho$, a median paired difference of at least $\delta$, and the same direction in at least a fraction $\kappa$ of rounds. The defaults are $\rho=5$, $\delta=100$ ms and $\kappa=0.8$.

**Confirmation and profiles.** Discovery uses three warm-up rounds and six timed rounds. A qualifying pair receives five warm-up rounds and twenty timed rounds in confirmation. It becomes a `candidate` when confirmation meets the same thresholds and retains the same slower query. Native query timeouts are recorded separately. After timing, GraphSymPerf archives both queries' native execution profiles and the construction metadata described under [graph state generation](#graph-state-generation). Plan review uses these records to locate repeated traversal, eager evaluation or other additional work.

**Scale response.** A relative-ratio threshold can miss a growing gap when both queries share substantial work. GraphSymPerf therefore applies a second screen to pairs recorded as `no_gap`. It rebuilds the same seeded case with $\max(1,\lfloor N/s\rfloor)$ expansion units, requires identical rendered queries, and measures the smaller graph. Let the slower query be the one with the larger median at scale $N$. Let $g_N$ and $g_{N/s}$ be the differences between its median and the other query's median at the two scales, using the same direction. The small-scale gap may be negative. The scale screen qualifies if

$$\frac{g_N}{\max(g_{N/s}, 1\,\text{ms})} \ge \gamma, \qquad g_N - g_{N/s} \ge \delta, \qquad \frac{\lvert\{i : \text{slower}_i > \text{faster}_i\}\rvert}{n} \ge \kappa,$$

where the last condition uses the rounds at scale $N$. A qualifying pair receives fresh confirmation measurements at both scales and becomes a `scale_candidate` when all three conditions hold again with the same slower query. The defaults are $s=10$ and $\gamma=3$; `scale_factor: null` disables this screen.

A short-circuit candidate directs plan review to the protected operand and its determinant. A symmetry candidate directs it to access paths, intermediate cardinalities and repeated work after reordering. The query pair, scale controls and execution profiles together support the diagnosis of the additional work.

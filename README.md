# GraphSymPerf

GraphSymPerf tests short-circuit and symmetry transformations on Neo4j. It generates a random symbolic query AST, inserts one compatible pattern, constructs one graph containing the required small and large regions, and compares a source query with one local mutation.

**Use a dedicated experiment database. Every executed case replaces all graph data in that database.** The graph and indexes remain unchanged between the two queries.

[中文说明](README.zh-CN.md)

## Requirements and setup

- Python 3.12.
- A Neo4j server supporting Cypher 25. This implementation was checked against Neo4j Community 2026.09.0.
- The dependencies in `requirements.txt`.

Install the Python dependencies:

    uv venv --python 3.12
    uv pip install -r requirements.txt

For a new local experiment database:

    docker run -d --name gsp-neo4j \
      -p 127.0.0.1:7687:7687 \
      -p 127.0.0.1:7474:7474 \
      -e NEO4J_AUTH=neo4j/change-this-local-password \
      neo4j:2026.09.0

Set the password and review `config.yaml` before running:

    export GRAPHSYMPERF_NEO4J_PASSWORD='change-this-local-password'
    .venv/bin/python main.py

The password is read from the named environment variable. It is not written into result files.

## Running and replaying

Run from the repository directory. All experiment settings are read from `config.yaml`:

    .venv/bin/python main.py

Choose the operation with `run.mode`:

| Mode | Behavior |
| --- | --- |
| `experiment` | Load each graph, compare results, and screen performance |
| `generate` | Generate and save cases without connecting to Neo4j |
| `check` | Load each graph and compare complete results without timing |

For the Y4 generation example, change these fields in the existing configuration:

    run:
      mode: generate
      patterns: [Y4]
      case_seed: 24
    graph:
      high_units: 100

Keep the other configuration fields. Then run the same command:

    .venv/bin/python main.py

`run.case_seed` selects an exact case and runs one round. Leave it `null` for a campaign, where `run.seed` controls pattern order and generated case seeds. Set `run.rounds` for the number of rounds, `run.minutes` for the time limit, and `run.save_all` to archive every constructed case. Change the connection address in `database.uri`.

To replay a saved case, set:

    replay:
      case: results/<run>/candidate/<case>
      check_only: true

Then run:

    .venv/bin/python replay.py

Replay uses the current `database` connection settings. With `replay.check_only: false`, it repeats the measurement protocol saved in the case archive.

A seed fixes the random choices in the current implementation. A selected seed may have no compatible position for a selected pattern; the program reports a skip. Replay uses the saved graph and query text and does not regenerate them from the seed.

## Main flow

Read `main.py`, starting with `prepare_case()`, to follow query construction:

1. `generate_ast()` creates the host query without receiving a pattern ID.
2. `choose_site()` selects a compatible expression, match, or query position.
3. `pattern.embed()` inserts the source form and prepares the mutation.
4. `build_graph()` creates a random background, prepares anchors, and applies the fixed graph recipe.
5. `instantiate()` binds symbolic names and values.
6. `pattern.mutate()` changes the selected local region.
7. `check_pair()` checks scopes and the output interface.
8. `Neo4j.load()` loads one graph; `run_pair()` compares and measures both queries.

Each round shuffles the 16 patterns and attempts one case per pattern. An incompatible context is skipped. Unexpected implementation or database errors stop the run.

## Source layout

| Location | Responsibility |
| --- | --- |
| `main.py` | Configuration, scheduling, and the main flow |
| `ast_nodes/` | Query, clause, expression, and graph-match nodes; Cypher rendering |
| `data_structures/scope.py` | Variable visibility, imports, projections, and reference checks |
| `data_structures/graph.py` | Nodes, relationships, indexes, and budget distribution |
| `data_structures/case.py` | Case records, symbols, and instantiation |
| `query_generation/` | Random AST generation and compatible-site selection |
| `graph_generation/` | Random background data and graph-construction orchestration |
| `mutator/` | Pattern source forms, local graph recipes, and mutations |
| `databases/neo4j.py` | Graph loading, query execution, and profiling |
| `executor.py` | Complete-result comparison, timings, screening, and case archives |
| `replay.py` | Reconstruction and replay of archived cases |

`ast_nodes/patterns.py` represents Cypher node, relationship, and path syntax. Testing patterns are implemented in `mutator/`.

The organization follows the direct entry-point and AST/module style of [ValScope](https://github.com/linli1724647576/ValScope). The implementation is written for graph construction and performance testing; it does not parse or reuse ValScope's SQL code.

## Current pattern catalog

These IDs belong to the rewritten catalog. Historical files under `BUG list/` keep their original IDs.

| ID | Pattern | Mutation |
| --- | --- | --- |
| S1 | Decisive OR operand | Replace the protected low-cost operand with the high-cost operand |
| S2 | Decisive AND operand | Replace the protected operand under a false condition |
| S3 | Conditional expression | Replace an unselected result or a later CASE condition |
| S4 | Non-null prefix | Replace the argument after a non-null COALESCE prefix |
| S5 | Zero output demand | Replace the match under `LIMIT 0` |
| S6 | Bounded output demand | Replace a UNION ALL branch after a sufficient witness branch |
| S7 | Existence | Replace a small matching region; includes EXISTS, NOT EXISTS, and a UNION witness |
| S8 | Empty input | Replace a mandatory downstream match after a false filter |
| Y1 | Conjunction symmetry | Swap the AND operands |
| Y2 | Disjunction symmetry | Swap the OR operands |
| Y3 | Single-edge reflection | Reverse the written endpoints while matching the same relationships |
| Y4 | Bounded-path reflection | Reverse the path and preserve reversal-invariant observations |
| Y5 | Independent components | Swap two independent mandatory matches |
| Y6 | Shared-anchor branches | Swap mandatory branches sharing an anchor |
| Y7 | Chain joins | Swap two mandatory matches connected through an intermediate node |
| Y8 | Independent optional branches | Swap two optional branches with only a mandatory anchor in common |

The eight symmetry entries are concrete testing patterns, not eight independent algebraic laws. Y5–Y7 share a join-commutativity basis but use different graph structures.

The decisive-operand pool in `mutator/operands.py` includes constants, comparisons, small-region counts, existence checks, and eligible outer-node properties. Each data-dependent option has a matching graph preparation step. Extending a pool does not introduce a new pattern.

## Generation and construction boundaries

The generator includes MATCH, OPTIONAL MATCH, WITH, RETURN, CALL, UNWIND, UNION, UNION ALL, conditional queries, scalar expressions, basic aggregates, lists, membership, list indexing, ordering, pagination, and nested EXISTS/COUNT/COLLECT subqueries. Scalars include integers, floats, booleans, and strings. Recursion is bounded by the configured AST and subquery budgets.

It samples a finite implemented grammar. It does not uniformly sample all Cypher, parse arbitrary user queries, or solve arbitrary graph constraints.

The random background uses symbols from the host AST. Pattern-specific labels and relationship types are allocated separately. Shared AST symbols keep the same binding.

Correlated construction uses non-null node variables with a traceable label source. It considers the candidate anchor set, not a guessed single runtime binding. Opaque or nullable sources are not selected for recipes that require such anchors.

The graph builder prepares the pattern's local conditions. It does not solve every outer filter or guarantee that the selected operation is reached. An empty result may be expected, especially for S5 and S8.

`high_units` is the total number of high-cost expansion units for a case. It is divided across applicable anchors or regions. A path unit can contain several nodes and relationships. Background data, small regions, and fixed witnesses are additional; archives report the actual totals.

Paths are compared in their original order. Path reflection is rejected when the enclosing query directly observes the changed path order. List order and duplicate result rows are also preserved.

The two queries share one graph and one index state. Another case can rebuild the dedicated database. Temporary loading indexes are removed before measurement; case indexes are then created and awaited.

## Measurement defaults

| Setting | Default |
| --- | ---: |
| Background nodes | 50 |
| Background relationships | 100 |
| High-cost expansion units | 100,000 |
| Discovery warmups per query | 3 |
| Discovery pairs | 6 |
| Confirmation warmups per query | 5 |
| Confirmation pairs | 20 |
| Minimum runtime ratio | **5.0** |
| Minimum median paired difference | **100 ms** |
| Required direction consistency | 0.8 |
| Query timeout | 10 seconds |

Every sample consumes the complete result. Comparison uses typed row multisets: booleans and integers remain distinct, entities retain their identities, and lists and paths retain their internal order.

Discovery and confirmation alternate AB/BA execution. Both rounds must meet the configured ratio, absolute difference, and direction-consistency thresholds, with the same slower query. Timings include query execution, result transfer, and client-side result consumption; graph loading and result normalization are outside the timing interval. PROFILE is collected separately.

A confirmed timing gap is a performance candidate, not proof of a missed optimization. Plans and workload counts are needed to explain it. A timeout is recorded as a timeout, not converted into a measured runtime or ratio.

## Results

Each run writes `summary.csv`. Ordinary cases retain their ID, seed, status, sizes, and available measurements there. Candidates, result mismatches, and timeouts also retain:

    results/<run>/<status>/<case>/
      query_a.cypher
      query_b.cypher
      graph.json
      result.json
      plans.json

`plans.json` is written when profiling completes. `run.save_all: true` also archives ordinary executed cases. `run.mode: generate` always archives generated cases.

Result statuses include:

- `skipped`: no compatible position or the fixed recipe is inapplicable.
- `equivalent`: complete results matched in check mode.
- `no_gap`: discovery did not meet the performance thresholds.
- `unconfirmed`: the discovery gap did not survive confirmation.
- `candidate`: discovery and confirmation both passed.
- `result_mismatch`: complete results differed; the pair is excluded from performance screening.
- `timeout`: a database transaction reached its timeout.

## Submitted issue registry

[bugs.csv](bugs.csv) is the single maintained registry of submitted issues. Each row is one unique GitHub issue URL. Update that row when its status changes; add a row when a new issue is submitted. Historical reports remain under `BUG list/`, including local reports that have not been submitted.

Pattern mappings use the current S1–S8 and Y1–Y8 catalog, introduced in commit `26f62dc`. They are based on the submitted queries and graph conditions, not the historical directory name or issue title.

- `pattern_ids` contains direct semantic matches; `pattern_count` is the number of distinct IDs in that field. A match identifies a catalog symmetry or a short-circuit condition exercised by the reported pair. It does not claim that the current generator emits the exact report, or that its backend is implemented.
- `related_pattern_ids` and `related_pattern_count` record a shared optimization mechanism without the catalog transformation. These are excluded from direct counts. Boolean identity insertion, compound reorderings, and explicit CASE guard rewrites can fall here.
- `mapping_class` is `direct`, `related_only`, or `outside_catalog`. `mapping_notes` explains the query-level decision. IDs are separated by semicolons; an empty ID field has count zero.
- `status` records the current disposition; `reported_status` preserves the status supplied when the row was registered. Values are `pending`, `confirmed`, `fixed`, `uninterested` (explicitly declined), and `not_a_bug` (upstream rejects the attribution to that DBMS).
- `status_basis`, `status_evidence`, and `status_notes` distinguish an upstream reply from a user-provided status. Closed issues are not automatically fixed. Automated reproduction is not treated as human confirmation; version-specific fixes remain qualified in the notes.
- `github_state` and `upstream_type` preserve GitHub's separate state and issue-type fields. `bug_type` retains the submitter's Performance BUG / Logical BUG classification. `duplicate_of` records upstream grouping; `last_checked` records the audit date.

Count unique issue URLs for submitted-issue totals. For each pattern, count rows containing its ID in `pattern_ids`; one issue can contribute to several pattern counts. Neither total measures independent root causes. In particular, ArcadeDB #10307 and #10308 are separate submissions grouped upstream under #10309.

## Validation and retained material

The rewrite uses direct checks against a dedicated Neo4j instance rather than restoring the former test framework. Local acceptance artifacts are kept under `results/validation-*`; they are not part of the source package.

Historical material remains under `paper/`, `BUG list/`, `reference/`, and `picture/`. It may use older pattern IDs or methods. The README and current source describe this implementation; `bugs.csv` records submitted-issue status and current pattern mappings.

Only the Neo4j interface is implemented. Supporting another system requires checking its grammar, result semantics, and pattern prerequisites; adding a connection class alone is not sufficient.

# Bug reports

The [registry](../bugs.csv) records submitted reports, including their public issue links, statuses, supporting evidence and pattern assignments. The reports in [bug-reports/](../bug-reports/README.md) provide graph setup statements, query pairs, results and measurements.

## Report structure

Each performance report contains four sections:

1. **Describe the situation**: present the query pair and explain the observed difference.
2. **Which DBMS versions are affected?**: identify the tested version and relevant settings.
3. **How to reproduce**: provide the complete graph setup, returned results, timing method and execution-plan excerpts.
4. **Expected performance**: identify the work the database could avoid, using the measured comparison.

Keep queries, graph scale, results, runtimes and plans tied to the same experiment. The affected-version section identifies the version used for the displayed measurements. The registry records the earliest affected version supported by execution or source evidence in `introduced_version`.

## Statuses

| Status | Meaning |
| --- | --- |
| `pending` | Submitted and awaiting a recorded confirmation or resolution. |
| `confirmed` | Recorded as confirmed; `status_basis` identifies the source. |
| `fixed` | Recorded as fixed; `status_evidence` identifies the supporting statement or change. |
| `uninterested` | The developer declined to pursue the report or indicated that changes are unlikely. |
| `not_a_bug` | The cited review rejected the reported database attribution. |
| `archived` | A submitted report is retained as a historical record after review. |

`last_checked` gives the status check date. Developer replies and merged fixes support upstream confirmation and repair; author-provided classifications retain their source in `status_basis`. Pattern identifiers follow the [pattern catalog](pattern-catalog.en.md).

## Registry fields

`bugs.csv` has one row per submitted report. Each row includes its public issue identifier and URL. Empty optional fields mean that the value has not been recorded. Pattern lists use semicolons.

| Field | Meaning |
| --- | --- |
| `dbms` | Database project or shared query engine associated with the report. |
| `issue_number` | Public issue identifier, including the project prefix for Apache JIRA issues. |
| `issue_url` | URL of the submitted public issue. |
| `title` | Description of the reported behavior. |
| `bug_type` | Category of the reported defect, such as performance or logical correctness. |
| `language` | Query language used by the report. |
| `status` | Classification defined in the status table above. |
| `reported_status` | Status supplied by the author or source inventory. |
| `github_state` | Tracker state at the recorded check date, such as `open` or `closed`. |
| `upstream_type` | Issue type assigned by the upstream tracker. |
| `pattern_ids` | SC/SY patterns associated with the report. |
| `pattern_count` | Number of identifiers in `pattern_ids`. |
| `mapping_notes` | Explanation of the pattern assignments or the absence of an assignment. |
| `status_basis` | Source category for the status, such as a developer reply, merged fix or author-provided inventory. |
| `status_evidence` | Public comment, issue, change or repository path supporting the status. |
| `status_notes` | Explanation of the status and relevant follow-up. |
| `duplicate_of` | Representative or related issue/report; the notes explain the relationship. |
| `last_checked` | Date of the status check, in `YYYY-MM-DD` format. |
| `bug_latency_days` | UTC calendar-day interval from the supported affected release, build or source-snapshot date to the public report date. This gives a lower bound on how long the behavior existed before reporting. |
| `F` | `1` when evidence places the behavior in the project's first public release or earliest documented build; `0` when this classification is not assigned. |
| `FSFS` | `1` when evidence places the behavior in the first version supporting the relevant feature or syntax, including milestone releases; `0` when this classification is not assigned. |
| `introduced_version` | Earliest affected version supported by execution or source evidence. |
| `local_report` | Path to the report included in this repository. |

<!-- BEGIN REPORT INDEX -->
## Apache AGE

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [2428](https://github.com/apache/age/issues/2428) · Missed optimization: NULL contradiction disjunct adds Apache AGE filter rows | `not_a_bug` | SC2, SY2 | — |
| [2480](https://github.com/apache/age/issues/2480) · A redundant length(p) = 2 check makes a fixed two-hop path about 11x slower | `confirmed` | SY4 | — |
| [2481](https://github.com/apache/age/issues/2481) · A redundant type(r) = 'P' check makes a statically typed relationship query about 6x slower | `pending` | SY3 | — |

## ArcadeDB

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [4405](https://github.com/ArcadeData/arcadedb/issues/4405) · Missed optimization: redundant AND-true OR-false branch raises ArcadeDB PROFILE rows | `fixed` | SC2, SY2 | — |
| [5116](https://github.com/ArcadeData/arcadedb/issues/5116) · Result-equivalent queries show a 16.2x runtime gap across pattern-order variants | `fixed` | SY5, SY6 | — |
| [5117](https://github.com/ArcadeData/arcadedb/issues/5117) · Reordering independent MATCH patterns in ArcadeDB leads to a 33.4x runtime gap | `fixed` | SY5 | — |
| [5360](https://github.com/ArcadeData/arcadedb/issues/5360) · Bare double NOT acts as single NOT in an OpenCypher WHERE clause | `fixed` | — | — |
| [5362](https://github.com/ArcadeData/arcadedb/issues/5362) · A duplicate equality predicate changes NodeIndexSeek to NodeByLabelScan in OpenCypher | `fixed` | SY1 | — |
| [5363](https://github.com/ArcadeData/arcadedb/issues/5363) · A label predicate in WHERE scans unrelated vertex types in OpenCypher | `fixed` | — | — |
| [9579](https://github.com/ArcadeData/arcadedb/issues/9579) · COUNT subquery runs before a false AND condition | `fixed` | SC2, SY1 | [Reproducer](../bug-reports/arcadedb/9579.md) |
| [9580](https://github.com/ArcadeData/arcadedb/issues/9580) · COALESCE evaluates a COUNT after a non-null constant | `fixed` | SC4 | [Reproducer](../bug-reports/arcadedb/9580.md) |
| [10307](https://github.com/ArcadeData/arcadedb/issues/10307) · Gremlin scans neighbors before an always-empty filter | `fixed` | SC5 | [Reproducer](../bug-reports/arcadedb/10307.md) |
| [10308](https://github.com/ArcadeData/arcadedb/issues/10308) · Gremlin materializes all matching neighbors before an existence test | `fixed` | SC7 | [Reproducer](../bug-reports/arcadedb/10308.md) |
| [11081](https://github.com/ArcadeData/arcadedb/issues/11081) · Read-only EXISTS with LIMIT 0 still evaluates its input | `fixed` | SC5 | [Reproducer](../bug-reports/arcadedb/11081.md) |
| [11082](https://github.com/ArcadeData/arcadedb/issues/11082) · Read-only CALL UNION ALL materializes branches before LIMIT 3 | `fixed` | SC6 | [Reproducer](../bug-reports/arcadedb/11082.md) |
| [11083](https://github.com/ArcadeData/arcadedb/issues/11083) · Swapping independent OPTIONAL MATCH clauses repeats the same bound-root lookup | `fixed` | SY6, SY7, SY8 | [Reproducer](../bug-reports/arcadedb/11083.md) |

## FalkorDB

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [2081](https://github.com/FalkorDB/FalkorDB/issues/2081) · Missed optimization: double-negated true conjunct raises FalkorDB PROFILE rows | `fixed` | SC1, SY2 | — |
| [3227](https://github.com/FalkorDB/FalkorDB/issues/3227) · Repeated traversal after swapping independent OPTIONAL MATCH clauses | `confirmed` | SY8 | [Reproducer](../bug-reports/falkordb/3227.md) |
| [3228](https://github.com/FalkorDB/FalkorDB/issues/3228) · Cartesian product scans millions of targets before an empty index lookup | `confirmed` | SC8, SY5 | [Reproducer](../bug-reports/falkordb/3228.md) |
| [3315](https://github.com/FalkorDB/FalkorDB/issues/3315) · ALL over path nodes traverses branches behind a failed node | `confirmed` | SC2, SY4 | — |
| [3637](https://github.com/FalkorDB/FalkorDB/issues/3637) · Reversing endpoint order in a single undirected path drops the index scan | `confirmed` | SY4 | [Reproducer](../bug-reports/falkordb/3637.md) |

## JanusGraph

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [5113](https://github.com/JanusGraph/janusgraph/issues/5113) · Path-node checks do not prune failed repeat prefixes | `pending` | SC2, SY4 | [Reproducer](../bug-reports/janusgraph/5113.md) |
| [5148](https://github.com/JanusGraph/janusgraph/issues/5148) · LIMIT(1) still preloads the complete adjacency list | `pending` | SC6, SC7 | [Reproducer](../bug-reports/janusgraph/5148.md) |
| [5147](https://github.com/JanusGraph/janusgraph/issues/5147) · An edge check against a bound match() vertex fetches the whole adjacency list | `pending` | SY3 | [Reproducer](../bug-reports/janusgraph/5147.md) |

## LadybugDB

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [668](https://github.com/LadybugDB/ladybug/issues/668) · Result-equivalent LadyBugDB queries show a 17.1x runtime gap across double-negated boolean-redundancy variants | `fixed` | SC1, SY2 | — |
| [669](https://github.com/LadybugDB/ladybug/issues/669) · Result-equivalent LadyBugDB queries show a 8.0x runtime gap across excluded-middle boolean-redundancy variants | `fixed` | SC1, SY2 | — |
| [696](https://github.com/LadybugDB/ladybug/issues/696) · Bug: Reordering comma-separated `MATCH` patterns can ignore a label constraint on a previously bound variable | `fixed` | SY6 | — |
| [697](https://github.com/LadybugDB/ladybug/issues/697) · Bug: Repeating a bound node in a comma-separated OPTIONAL MATCH doubles the result cardinality | `fixed` | SY8 | — |
| [698](https://github.com/LadybugDB/ladybug/issues/698) · NULL contradiction scans the node table instead of producing an empty result | `fixed` | SC2, SC8 | — |
| [720](https://github.com/LadybugDB/ladybug/issues/720) · Bug: ORDER BY before LIMIT and count(*) causes a large slowdown | `fixed` | SC6 | — |
| [721](https://github.com/LadybugDB/ladybug/issues/721) · Bug: WITH DISTINCT on a primary-key property causes a large slowdown | `fixed` | — | — |
| [1483](https://github.com/LadybugDB/ladybug/issues/1483) · Bug: COALESCE executes a COUNT subquery after a non-NULL constant | `fixed` | SC4 | [Reproducer](../bug-reports/ladybugdb/1483.md) |

## Memgraph

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [4208](https://github.com/memgraph/memgraph/issues/4208) · Guarded contradiction branch raises Memgraph DB hits | `confirmed` | SC2, SY2 | — |
| [4456](https://github.com/memgraph/memgraph/issues/4456) · Static subscript property access does not use a property index | `pending` | — | — |
| [4457](https://github.com/memgraph/memgraph/issues/4457) · An exact zero-hop path performs unnecessary variable-length expansion work | `pending` | SY4 | — |
| [4458](https://github.com/memgraph/memgraph/issues/4458) · Moving a relationship type to WHERE type(r) causes a large slowdown | `pending` | SY3 | — |
| [4459](https://github.com/memgraph/memgraph/issues/4459) · Double negation prevents an indexed equality from using the property index | `fixed` | — | — |
| [5091](https://github.com/memgraph/memgraph/issues/5091) · Repeated expansion after swapping two MATCH clauses | `pending` | SY6 | [Reproducer](../bug-reports/memgraph/5091.md) |

## Neo4j

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [13783](https://github.com/neo4j/neo4j/issues/13783) · Redundant always-false disjunct adds a Filter and inflates label-scan work | `fixed` | SC2, SY2 | — |
| [13813](https://github.com/neo4j/neo4j/issues/13813) · Semantically equivalent comma-pattern reorder inflates all-node scan work | `uninterested` | SY5 | — |
| [13814](https://github.com/neo4j/neo4j/issues/13814) · NULL-and-NOT-NULL disjunct changes access order and will lead to performance degradation | `uninterested` | SC2, SY2 | — |
| [13864](https://github.com/neo4j/neo4j/issues/13864) · count(1) scans all matched nodes instead of using the count store | `uninterested` | — | — |
| [13865](https://github.com/neo4j/neo4j/issues/13865) · An exact *0..0 path scans all nodes and runs VarLengthExpand | `uninterested` | SY4 | — |
| [13866](https://github.com/neo4j/neo4j/issues/13866) · Swapping read-only UNION branches changes a later numeric result | `confirmed` | — | — |
| [13867](https://github.com/neo4j/neo4j/issues/13867) · Conditional count does not use an indexable predicate | `uninterested` | SC3 | — |
| [14007](https://github.com/neo4j/neo4j/issues/14007) · Unnecessary traversal with AND and EXISTS | `uninterested` | SC2, SC3, SY1 | [Reproducer](../bug-reports/neo4j/14007.md) |
| [14008](https://github.com/neo4j/neo4j/issues/14008) · Performance difference between two undirected path queries | `fixed` | SC2, SY4 | [Reproducer](../bug-reports/neo4j/14008.md) |
| [13756](https://github.com/neo4j/neo4j/issues/13756) · Estimated row count and DB Hits increase after adding a relationship type | `archived` | — | — |
| [13755](https://github.com/neo4j/neo4j/issues/13755) · Estimated row count increases after adding a stricter label | `archived` | — | — |

## TinkerPop

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [TINKERPOP-3316](https://issues.apache.org/jira/browse/TINKERPOP-3316) · A redundant null test prevents the count-to-existence optimization | `pending` | SC7 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3316.md) |
| [TINKERPOP-3315](https://issues.apache.org/jira/browse/TINKERPOP-3315) · A wrapped vertex filter misses early rejection | `pending` | SC2, SY1 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3315.md) |
| [TINKERPOP-3311](https://issues.apache.org/jira/browse/TINKERPOP-3311) · A constant-false child filter still expands every neighbor | `confirmed` | SC5 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3311.md) |
| [TINKERPOP-3312](https://issues.apache.org/jira/browse/TINKERPOP-3312) · and()/or() evaluate an expensive child before a cheap has() child | `pending` | SY1, SY2 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3312.md) |
| [TINKERPOP-3313](https://issues.apache.org/jira/browse/TINKERPOP-3313) · match() re-runs an independent pattern for every traverser of the other pattern | `confirmed` | SY5, SY6 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3313.md) |
| [TINKERPOP-3314](https://issues.apache.org/jira/browse/TINKERPOP-3314) · Bounded reachability in match() expands from the high-degree endpoint | `confirmed` | SY4 | [Reproducer](../bug-reports/tinkerpop/TINKERPOP-3314.md) |

## HugeGraph

| Report | Status | Patterns | Local report |
| --- | --- | --- | --- |
| [3479](https://github.com/apache/hugegraph/issues/3479) · [Bug] Gremlin enumerates 100,001 incoming neighbors to check an edge between bound vertices | `pending` | SY3 | [Reproducer](../bug-reports/hugegraph/3479.md) |
| [3480](https://github.com/apache/hugegraph/issues/3480) · [Bug] Gremlin not(out().has()) is slower on an edge label with more matching neighbors | `pending` | SC7 | [Reproducer](../bug-reports/hugegraph/3480.md) |
<!-- END REPORT INDEX -->

## Maintaining the reports

Update issue links, status evidence, pattern assignments and report paths in `bugs.csv`. Keep each report's setup, query pair and measurements together in its Markdown file. After editing the registry, regenerate the README tables and report index:

```sh
python scripts/update_readme.py
```

# Local experiment record: detection-20261009

Base revision: dfce945 on codex/scor-pattern-experiments. The experiment adds the absolute-difference gate and Memgraph post-load statistics preparation; it does not introduce a new application framework.

Scope: Memgraph, FalkorDB, ArcadeDB, Apache AGE, LadybugDB, JanusGraph, and Apache HugeGraph. Neo4j's existing reports were reorganized only. The verified inventory contains two report IDs per tested DBMS, 14 in total. IDs are scoped to the DBMS; common implementation causes require cross-DBMS deduplication.

Selection: complete identical result bags, ordinary paired runtime ratio >=5, paired difference >100 ms, two independently rebuilt confirmation states (20 balanced AB/BA pairs each), CI lower bound >=5, >=95% timing direction, and paired difference above the recorded AA/BB noise. Native plan and avoidable-work review are required after the timing gate. Warm-ups, limits and controls are preserved in each source and report.

Environment: environment.json records pinned images, architecture, memory limits, ports and JVM options. Database versions and exact selected query coordinates are in issue_inventory.json. Native protocols also preserve schema/index/state checks and full explain/profile data. Profiling is outside ordinary runtime samples.

Evidence: original case directories contain setup and actual queries; native probe.json files contain complete input recipes, samples and diagnostics. registered/ contains compact manually reviewed annotations and portable native reproducer companions. experiment/ retains original helper/config/log provenance and a portable inventory validator. No temporary database password values or environment files are included.

Classification: built-in P/S reports use BUG list/<DBMS>/<Pattern>/; native mechanism reports use BUG list/<DBMS>/Native/<mechanism>/. Their contributions remain distinct in the existing summary tool. This directed search did not allocate equal Pattern budgets, so no discovery rate, bugs/hour or marginal Pattern contribution is inferred.

Exclusions: EXCLUDED.json marks invalid or supporting scopes. Unconfirmed noise, resource termination, incomplete preparation, normal explicit traversal-order effects, and superseded comparison forms are not registered. The inventory is derived from the final report queries and cannot substitute a more significant reference comparison.

Outcome at experiment completion: registered/bugs.csv preserves 14 local reproduced entries and blank upstream URLs. The root bugs.csv now maintains the project registry and subsequent user-provided upstream dispositions; linked entries retain their original case identities. This experiment did not submit upstream reports or establish a cross-DBMS unique source-root count.

Validation: 1185 offline tests passed, 242 explicitly opt-in integration tests were deselected; actual database execution evidence is the independent paired experiment, state and literal reproduction records. The temporary containers and their volumes are removed after all reports and raw evidence are saved; cached images remain available for reproduction.

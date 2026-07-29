# Architecture

`ExperimentSpec -> RunPlan -> ShardSupervisor -> GDsmith base graph -> involutive symmetric state -> certified graph-pattern pairs -> runtime screen -> independent validation -> paired reducer -> SubmitReadyIssue`.

The formal worker does not transform arbitrary GDsmith queries. It derives both graph copies and every query pair from the parsed GDsmith topology, validates complete typed results on the target DBMS, and retains plans only for post-candidate explanation. Leaf layers do not import CLI, app, or adapters.

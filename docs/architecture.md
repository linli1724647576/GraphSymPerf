# Architecture

`ExperimentSpec -> RunPlan -> ShardSupervisor -> GDsmith corpus -> SafetyCertificate pair -> runtime screen -> independent validation -> paired reducer -> SubmitReadyIssue`. Leaf layers do not import CLI, app, or adapters.

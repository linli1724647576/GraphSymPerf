# Project context

The formal experiment is the single schema-v2 `config/experiment.yaml`: seven GDBMSs, sixteen shards each, and 1,000 accepted query pairs per shard. GDsmith is the only base-graph generator. GraphSymPerf derives certificate-carrying graph-state symmetry pairs from that graph; Boolean redundancy and general-purpose equivalent-query rewriting are outside the formal path. Runtime evidence determines candidates; plan, PROFILE and PMC are post-validation explanation only.

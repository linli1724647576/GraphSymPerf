# Operations

Use `experiment doctor`, then `experiment plan`, then `experiment run`. Each shard emits heartbeats, append-only events and atomic checkpoints; retry exhaustion creates a reliability finding without stopping other shards.

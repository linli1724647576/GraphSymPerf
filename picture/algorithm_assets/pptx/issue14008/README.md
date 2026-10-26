# Real running example: Neo4j #14008

Source: [Performance difference between two undirected path queries](https://github.com/neo4j/neo4j/issues/14008).

**Algorithm 1 caption.** Graph-state construction for P3. The indexed anchor has a valid two-hop witness and a second branch entering a node with `keep=false`. That node precedes 300,000 two-hop branches. Node numbers denote `uid` values; unnumbered circles abbreviate repeated targets. The graph specification and its construction conditions are passed to Algorithm 2.

**Algorithm 2 caption.** The real P3 instance is populated and mirrored while retaining its predicate and projection. Dashed nodes are placeholders. The lower binding diagrams preserve node-pattern labels and constraints, rather than binding to one result node. The lower witness graphs show the accepted path in each query's written order. Ticks denote `keep=true`; the result tables retain the same column order and row. Runtime values are the medians reported for Neo4j 2026.09.0.

Both plans start at indexed `u`. The diagrams illustrate construction and query representation, not physical expansion direction. This case exercises the symmetry branch only; it has no reference region R. Source metadata, the current generator's matching query pair and slide notes preserve the evidence and version boundaries. No database execution was performed for this figure update.

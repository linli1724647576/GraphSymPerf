I observed the same comma-pattern-order performance sensitivity on Neo4j Community 2026.05.0 with a different query and graph. This is follow-up regression evidence for this issue, not a claim of a distinct root cause.

The two queries return identical complete results and differ only in the order of two comma-separated pattern components in the same `MATCH` clause.

Fast query (A):

```cypher
MATCH (n0 :L3)<-[r0 :T2]-(n1 :L2)<-[r1 :T0]-(n2) WHERE (((r0.id) > -1) AND ((r0.id) <> (r1.id))) WITH r0, r1, (r1.k48) AS a0 WHERE (a0 ENDS WITH (r1.k48)) UNWIND [(r0.k56), (r0.k56), 87662976] AS a1 UNWIND [199551179] AS a2 MATCH (n4 :L0)-[r3 :T2]->(n5 :L3), (n8 :L0) WHERE (NOT (r0.k57)) WITH a1, r1, n8, avg(353308392) AS a3 WITH r1, n8, a3 UNWIND [-673213226, a3, -955986132] AS a4 OPTIONAL MATCH (n9 :L5 :L0)-[r6 :T4]->(n10)-[r7 :T0]->(n11 :L5 :L3 :L4) WHERE (((r6.id) > -1) AND ((r6.id) <> (r7.id))) RETURN DISTINCT a3, a4
```

Slow query (B):

```cypher
MATCH (n0 :L3)<-[r0 :T2]-(n1 :L2)<-[r1 :T0]-(n2) WHERE (((r0.id) > -1) AND ((r0.id) <> (r1.id))) WITH r0, r1, (r1.k48) AS a0 WHERE (a0 ENDS WITH (r1.k48)) UNWIND [(r0.k56), (r0.k56), 87662976] AS a1 UNWIND [199551179] AS a2 MATCH (n8 :L0), (n4 :L0)-[r3 :T2]->(n5 :L3) WHERE (NOT (r0.k57)) WITH a1, r1, n8, avg(353308392) AS a3 WITH r1, n8, a3 UNWIND [-673213226, a3, -955986132] AS a4 OPTIONAL MATCH (n9 :L5 :L0)-[r6 :T4]->(n10)-[r7 :T0]->(n11 :L5 :L3 :L4) WHERE (((r6.id) > -1) AND ((r6.id) <> (r7.id))) RETURN DISTINCT a3, a4
```

Environment and graph:

- Neo4j Community 2026.05.0
- 1,280 nodes and 2,237 relationships
- ordinary query execution, not `PROFILE`, was used as the runtime oracle

Independent validation used two fresh Neo4j process sessions. Each session ran 20 balanced A/B pairs; the slower direction was consistent in all pairs, and same-query negative controls passed.

| Session | A median | B median | Ratio | Median delta | Ratio CI lower bound | Delta CI lower bound |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 43.425 ms | 632.085 ms | 14.524x | 588.337 ms | 14.366x | 585.844 ms |
| 2 | 42.802 ms | 608.576 ms | 14.485x | 565.876 ms | 14.191x | 557.525 ms |
| Pooled | — | — | 14.505x | 586.099 ms | 14.361x | 573.711 ms |

I can provide the full generated 1,280-node graph snapshot if useful. The case has not yet been minimized, so I am posting it as additional version-regression evidence rather than as a separate issue.

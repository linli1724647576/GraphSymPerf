CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspAnchor) ON (n.uid);

CREATE (:GspAnchor {uid: 1, keep: true}), (:GspAnchor {uid: 2, keep: false})-[:GSP_EXPAND]->(:GspLeaf {uid: 4, mark: true}), (:GspAnchor {uid: 3});

MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 40000) AS i CREATE (u)-[:GSP_EXPAND]->(:GspLeaf {uid: 895440+i, mark: false});

MATCH (u:GspAnchor) WHERE coalesce(u.keep, false) = false MATCH (u)-[:GSP_EXPAND]->(v:GspLeaf) CREATE (u)-[:GSP_REFERENCE]->(v);

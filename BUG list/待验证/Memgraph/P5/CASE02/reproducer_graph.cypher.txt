CREATE INDEX ON :GspAnchor(uid);

CREATE (:GspAnchor {uid: 1, keep: true});

CREATE (:GspAnchor {uid: 2, keep: false})-[:GSP_EXPAND]->(:GspLeaf {uid: 3, mark: true});

MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 40000) AS i CREATE (u)-[:GSP_EXPAND]->(:GspLeaf {uid: 895440 + i, mark: false});

MATCH (u:GspAnchor) WHERE coalesce(u.keep, false) = false MATCH (u)-[:GSP_EXPAND]->(v:GspLeaf) CREATE (u)-[:GSP_REFERENCE]->(v);

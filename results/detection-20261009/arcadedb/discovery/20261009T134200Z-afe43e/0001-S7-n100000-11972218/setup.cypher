CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:ScorRoot) ON (n.id);

CREATE (:ScorRoot {id: 1, uid: 1, key: 1, expected_count: 400000});

MATCH (u:ScorRoot {id: 1}) UNWIND range(1, 400000) AS i CREATE (u)-[:GSP_SC_HIGH]->(:ScorTarget {uid: 895440+i, mark: true, key: 1});

MATCH (u:ScorRoot {id: 1}) UNWIND range(2, 4) AS i CREATE (u)-[:GSP_SC_REF]->(:ScorTarget {uid: i, mark: true, key: 1});

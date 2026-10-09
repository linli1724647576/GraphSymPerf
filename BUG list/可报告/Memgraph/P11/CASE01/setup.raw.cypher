CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspAnchor) ON (n.uid);

CREATE (u:GspAnchor {uid: 1})-[:GSP_LOW]->(:GspLow {uid: 2}) CREATE (u)-[:GSP_HIGH]->(:GspHigh {uid: 3, keep: true});

MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 200000) AS i CREATE (u)-[:GSP_HIGH]->(:GspHigh {uid: 895440+i, keep: false});

MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 7) AS i CREATE (u)-[:GSP_LOW]->(:GspLow {uid: -i});

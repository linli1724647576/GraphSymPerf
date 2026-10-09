CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspLow) ON (n.id);

CREATE (:GspLow {uid: 1, id: 1})-[:GSP_LINK]->(:GspMiddle {uid: 2})-[:GSP_LINK]->(:GspHigh {uid: 3});

UNWIND range(1, 10000) AS i CREATE (:GspLow {uid: 895440+3*i, id: 895440+i})-[:GSP_LINK]->(:GspMiddle {uid: 895440+3*i+1})-[:GSP_LINK]->(:GspHigh {uid: 895440+3*i+2});

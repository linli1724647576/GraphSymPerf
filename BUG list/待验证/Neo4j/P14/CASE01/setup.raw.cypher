CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspLow) ON (n.uid);

CREATE (:GspLow {uid: 1}), (:GspHigh {uid: 2, keep: true});

UNWIND range(1, 40000) AS i CREATE (:GspHigh {uid: 895440+i, keep: false});

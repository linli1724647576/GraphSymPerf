CREATE INDEX ON :GspLow(id);

CREATE (:GspLow {uid: 1, id: 0}), (:GspHigh {uid: 2})-[:GSP_LINK]->(:GspTarget {uid: 3, keep: true});

UNWIND range(1, 20000) AS i CREATE (b:GspHigh {uid: 895440+5*i}) WITH b, i UNWIND range(1, 4) AS j CREATE (b)-[:GSP_LINK]->(:GspTarget {uid: 895440+5*i+j, keep: false});

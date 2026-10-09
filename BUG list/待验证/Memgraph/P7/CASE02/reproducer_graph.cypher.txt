CREATE INDEX ON :GspRare(uid);

CREATE (:GspRare:GspCommon {uid: 1})-[:GSP_LINK]->(:GspTarget {uid: 2});

UNWIND range(1, 10000) AS i CREATE (:GspCommon {uid: 895440+2*i})-[:GSP_LINK]->(:GspTarget {uid: 895440+2*i+1});

CREATE INDEX ON :GspLow(uid);

CREATE (:GspLow {uid: 1}), (:GspHigh {uid: 2, keep: true});

UNWIND range(1, 40000) AS i CREATE (:GspHigh {uid: 895440+i, keep: false});

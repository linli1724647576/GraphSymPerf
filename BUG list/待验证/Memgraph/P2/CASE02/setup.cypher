CREATE INDEX ON :GspLow(id);

CREATE (u:GspLow {uid: 1, id: 1})-[f:GSP_FORWARD {pair: 1}]->(v:GspHigh {uid: 2}) CREATE (v)-[:GSP_REVERSE {pair: 1}]->(u);

UNWIND range(1, 10000) AS i CREATE (u:GspLow {uid: 895440+2*i, id: 895440+i})-[:GSP_FORWARD {pair: i+1}]->(v:GspHigh {uid: 895440+2*i+1}) CREATE (v)-[:GSP_REVERSE {pair: i+1}]->(u);

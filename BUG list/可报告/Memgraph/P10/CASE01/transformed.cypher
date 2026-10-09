MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) MATCH (a:GspLow {id: 1}) RETURN a.uid, b.uid, c.uid;

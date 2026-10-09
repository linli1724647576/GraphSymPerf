MATCH (a:GspLow {id: 1}) MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (NOT (c.keep = false)) RETURN a.uid, b.uid, c.uid;

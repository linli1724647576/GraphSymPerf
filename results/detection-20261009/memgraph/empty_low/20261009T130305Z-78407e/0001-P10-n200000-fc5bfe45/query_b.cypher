MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (c.keep = true) MATCH (a:GspLow) WHERE (a.id = 1) RETURN a.uid, b.uid, c.uid;

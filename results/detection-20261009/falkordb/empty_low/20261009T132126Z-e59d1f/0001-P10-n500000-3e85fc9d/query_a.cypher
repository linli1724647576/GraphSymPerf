MATCH (a:GspLow) WHERE (a.id = 1) MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE (c.keep = true) RETURN a.uid, b.uid, c.uid;

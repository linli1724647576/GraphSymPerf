MATCH (u:GspLow) MATCH (u)-[:GSP_LINK]->(v:GspHigh) WHERE (u.id = 1) RETURN u.uid, v.uid;

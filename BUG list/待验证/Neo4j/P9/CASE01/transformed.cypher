MATCH (u:GspLow) WHERE (u.id = 1) MATCH (u)-[:GSP_LINK]->(v:GspHigh) RETURN u.uid, v.uid;

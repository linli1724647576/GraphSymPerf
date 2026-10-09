MATCH (u:GspLow)-[:GSP_LINK]->(v:GspHigh) WHERE (u.id = 1) RETURN u.uid, v.uid;

MATCH (u:GspLow)-[:GSP_LINK]->(v:GspHigh) WHERE (1 = u.id) RETURN u.uid, v.uid;

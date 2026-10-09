MATCH (u:GspLow)-[:GSP_FORWARD]->(v:GspHigh) WHERE (u.id = 1) RETURN u.uid, v.uid;

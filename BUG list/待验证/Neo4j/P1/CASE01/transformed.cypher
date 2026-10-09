MATCH (v:GspHigh)-[:GSP_LINK]-(u:GspLow {id: 1}) RETURN u.uid, v.uid;

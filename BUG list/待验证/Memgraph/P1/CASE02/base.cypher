MATCH (u:GspLow {id: 1})-[:GSP_LINK]-(v:GspHigh) RETURN u.uid, v.uid;

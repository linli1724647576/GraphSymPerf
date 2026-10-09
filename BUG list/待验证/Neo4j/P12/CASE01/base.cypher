MATCH (v)-[:GSP_LINK]->(w:GspHigh) MATCH (u:GspLow {id: 1})-[:GSP_LINK]->(v:GspMiddle) RETURN u.uid, w.uid;

MATCH (u:GspLow {id: 1})-[:GSP_LINK]->(v:GspMiddle) MATCH (v)-[:GSP_LINK]->(w:GspHigh) RETURN u.uid, w.uid;

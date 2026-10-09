MATCH (u:GspAnchor {uid: 1}) MATCH (u)-[:GSP_LOW]->(v:GspLow) MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (NOT (w.keep = false)) RETURN u.uid, v.uid, w.uid;

MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (w.keep = true) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) RETURN u.uid, v.uid, w.uid;

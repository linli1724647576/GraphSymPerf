MATCH (u:GspAnchor) OPTIONAL MATCH (u)-[:GSP_LOW]->(v:GspLow) OPTIONAL MATCH (u)-[:GSP_HIGH]->(w:GspHigh) WHERE (coalesce(w.keep, false) = true) RETURN u.uid, v.uid, w.uid;

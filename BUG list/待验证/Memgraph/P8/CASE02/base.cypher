MATCH (u:GspAnchor {uid: 1})-[:GSP_LOW|GSP_HIGH]->(v:GspTarget) WHERE (v.keep = true) RETURN u.uid, v.uid;

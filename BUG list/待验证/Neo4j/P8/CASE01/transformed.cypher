MATCH (u:GspAnchor {uid: 1})-[:GSP_HIGH|GSP_LOW]->(v:GspTarget) WHERE (v.keep = true) RETURN u.uid, v.uid;

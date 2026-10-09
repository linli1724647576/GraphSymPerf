MATCH (u:GspRare:GspCommon)-[:GSP_LINK]->(v:GspTarget) RETURN u.uid, v.uid;

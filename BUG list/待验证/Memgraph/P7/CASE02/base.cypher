MATCH (u:GspCommon:GspRare)-[:GSP_LINK]->(v:GspTarget) RETURN u.uid, v.uid;

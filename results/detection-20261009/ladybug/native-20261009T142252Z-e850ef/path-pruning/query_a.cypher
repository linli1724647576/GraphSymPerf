MATCH p=(u:ProbeRoot {uid:1})-[:ProbeA|ProbeB|ProbeC* TRAIL 3..3]->(v:ProbeLeaf) WHERE all(n IN nodes(p) WHERE n.keep=true) RETURN u.uid AS root_uid, v.uid AS leaf_uid, length(p) AS hops;

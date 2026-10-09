MATCH p=(u:ProbeRoot {uid:1})-[:ProbeA|ProbeB|ProbeC* TRAIL 3..3 (e,n | WHERE n.keep=true)]->(v:ProbeLeaf) WHERE u.keep=true RETURN u.uid AS root_uid, v.uid AS leaf_uid, length(p) AS hops;

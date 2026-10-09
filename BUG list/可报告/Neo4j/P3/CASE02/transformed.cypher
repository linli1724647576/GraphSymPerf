MATCH p = (v:GspHigh)-[:GSP_LINK*1..3]-(u:GspLow {id: 1}) WHERE all(x IN nodes(p) WHERE (x.keep = true)) RETURN u.uid, v.uid, length(p) AS hops;

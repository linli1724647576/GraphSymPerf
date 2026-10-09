MATCH p = (u:GspLow {id: 1})-[:GSP_LINK*1..3]-(v:GspHigh) WHERE all(x IN nodes(p) WHERE (x.keep = true)) RETURN u.uid, v.uid, length(p) AS hops;

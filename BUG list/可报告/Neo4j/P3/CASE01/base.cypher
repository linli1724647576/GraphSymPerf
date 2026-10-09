MATCH p = (u:GspLow)-[:GSP_LINK*1..3]-(v:GspHigh) WHERE ((u.id = 1) AND all(x IN nodes(p) WHERE (x.keep = true))) RETURN u.uid, v.uid, length(p) AS hops;

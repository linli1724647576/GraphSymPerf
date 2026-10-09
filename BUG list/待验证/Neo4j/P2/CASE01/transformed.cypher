MATCH (u:GspLow)<-[:GSP_REVERSE]-(v:GspHigh) WHERE (u.id = 1) RETURN u.uid, v.uid;

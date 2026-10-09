MATCH (u:ScorRoot {id: 1}) RETURN coalesce(null, 0, COUNT { MATCH (u)-[:GSP_SC_HIGH]->(v:ScorTarget) WHERE (v.mark = true) }) AS c0;

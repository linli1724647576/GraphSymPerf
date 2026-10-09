MATCH (u:ProbeRoot {uid:1}) RETURN coalesce(0, COUNT { MATCH (u)-[:ProbeA]->(m:ProbeMid)-[:ProbeB]->(h:ProbeHub)-[:ProbeC]->(l:ProbeLeaf) WHERE m.uid <> l.uid }) AS value;

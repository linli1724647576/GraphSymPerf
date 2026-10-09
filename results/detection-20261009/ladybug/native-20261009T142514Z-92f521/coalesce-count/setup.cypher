CREATE NODE TABLE ProbeRoot(uid INT64 PRIMARY KEY, keep BOOLEAN);

CREATE NODE TABLE ProbeMid(uid INT64 PRIMARY KEY, keep BOOLEAN);

CREATE NODE TABLE ProbeHub(uid INT64 PRIMARY KEY, keep BOOLEAN);

CREATE NODE TABLE ProbeLeaf(uid INT64 PRIMARY KEY, keep BOOLEAN);

CREATE REL TABLE ProbeA(FROM ProbeRoot TO ProbeMid);

CREATE REL TABLE ProbeB(FROM ProbeMid TO ProbeHub);

CREATE REL TABLE ProbeC(FROM ProbeHub TO ProbeLeaf);

CREATE (:ProbeRoot {uid:1,keep:true}), (:ProbeHub {uid:1,keep:true});

MATCH (u:ProbeRoot {uid:1}), (h:ProbeHub {uid:1}) UNWIND range(1,1500) AS i CREATE (u)-[:ProbeA]->(m:ProbeMid {uid:i,keep:true})-[:ProbeB]->(h);

MATCH (h:ProbeHub {uid:1}) UNWIND range(1,1500) AS i CREATE (h)-[:ProbeC]->(:ProbeLeaf {uid:1000000+i,keep:true});

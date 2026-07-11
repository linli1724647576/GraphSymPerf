MATCH (n0),
  (n1 :L4 :L1)<-[r0 :T4]-(n2 :L1)<-[r1 :T2]-(n3 :L2)
WHERE (((r0.id) > -1)
  AND ((r0.id) <> (r1.id)))
OPTIONAL MATCH (n0),
  (n4 :L2)-[r2 :T2]->(n3)<-[r3 :T0]-(n5 :L4 :L5),
  (n0)
WHERE ((r0.k61)
  AND ((r2.id) <> (r3.id)))
OPTIONAL MATCH (n0)
WHERE (r2.k47)
OPTIONAL MATCH (n4)-[]->(n3)<-[]-(n5 :L4)
WHERE (r0.k62)
WITH (n5.k32) AS a0,
  (n5.k27) AS a1,
  max('l') AS a2,
  max('x') AS a3,
  (n1.k22) AS a4
WHERE true
RETURN 3 AS a5

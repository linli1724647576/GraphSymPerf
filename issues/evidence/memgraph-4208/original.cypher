MATCH (n0 :L2 :L3 :L5)<-[r0 :T3]-(n1)<-[r1 :T1]-(n2 :L4), (n3)<-[r2 :T1]-(n4 :L6)
WHERE (((('dV' CONTAINS 'g3iIdVDF') AND ((r0.id) <> (r1.id))) AND ((r0.id) <> (r2.id))) AND ((r1.id) <> (r2.id)))
MATCH (n5 :L2 :L1 :L0)<-[r4 :T3]-(n3 :L6)
WHERE (NOT (n2.k29))
WITH n5, n2, (r2.k53) AS a0
ORDER BY (n2.k26), (n2.k28) DESC
WHERE ((n5.k12) STARTS WITH (n5.k12))
OPTIONAL MATCH (n6 :L4)-[r5 :T3]->(n0 :L3 :L5)<-[r6 :T2]-(n7 :L2)
WHERE (((r5.id) > -1) AND ((r5.id) <> (r6.id)))
WITH (r5.k69) AS a1, max('2dIcwz4jd') AS a2, (n6.k25) AS a3, (n2.k29) AS a4, r6
MATCH (n6)-[]->(n0)<-[]-(n7)
MATCH (n0 :L3)<-[]-(n1)<-[]-(n2 :L4)
WHERE ((n0.k21) OR (r6.k63))
WITH min('jRiRhrULI') AS a5, n1, r6, a1
ORDER BY (r6.k58) DESC
WHERE (NOT true)
WITH DISTINCT *
WHERE (a5 <> a5)
RETURN DISTINCT a5;

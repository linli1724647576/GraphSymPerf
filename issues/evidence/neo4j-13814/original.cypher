MATCH (n1 :L0), (n3)<-[r2 :T5]-(n4 :L3), (n5 :L2)<-[r3 :T6]-(n6)-[r4 :T1]->(n7)
WHERE (((((n5.k12) <> (r2.k74)) AND ((r2.id) <> (r3.id))) AND ((r2.id) <> (r4.id))) AND ((r3.id) <> (r4.id)))
OPTIONAL MATCH (n8)-[r5 :T2]->(n7 :L0), (n5 :L2)<-[r6 :T2]-(n9 :L0 :L6)
WHERE (((r5.id) > -1) AND ((r5.id) <> (r6.id)))
WITH max('jRhWTyh') AS a0, (r6.k55) AS a1
WITH a1, a0
WHERE (true AND true)
RETURN 1 AS a2;

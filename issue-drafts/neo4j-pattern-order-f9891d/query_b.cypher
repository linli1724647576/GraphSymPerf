MATCH (n3 :L5), (n0)-[r0 :T6]->(n1 :L1)
WHERE ('uv' CONTAINS '9uvWO')
MATCH (n3), (n3 :L5)
MATCH (n4 :L4)-[r2 :T5]->(n5 :L1)-[r3 :T1]->(n6), (n4)<-[r4 :T0]-(n7 :L4 :L2)-[r5 :T3]->(n8)
WHERE ((((((((n1.k7) OR (r4.k44)) AND ((r2.id) <> (r3.id))) AND ((r2.id) <> (r4.id))) AND ((r2.id) <> (r5.id))) AND ((r3.id) <> (r4.id))) AND ((r3.id) <> (r5.id))) AND ((r4.id) <> (r5.id)))
OPTIONAL MATCH (n4 :L4)<-[]-(n7 :L4)-[]->(n8), (n3)
WITH r3
ORDER BY (r3.k46) DESC
MATCH (n3 :L5), (n3)
WHERE ((n3.k36) < (r3.k47))
OPTIONAL MATCH (n0 :L2)-[]->(n1)<-[]-(n2)
RETURN (r3.k46) AS a0;

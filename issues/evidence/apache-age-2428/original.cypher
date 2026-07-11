MATCH (n0 :L2)-[r0 :T3]->(n1)<-[r1 :T4]-(n2 :L1), (n4 :L0)<-[r3 :T1]-(n5 :L1), (n6 :L3)-[r4 :T1]->(n7 :L3)-[r5 :T2]->(n3)
WHERE ((((((((((true AND ((r0.id) <> (r1.id))) AND ((r0.id) <> (r3.id))) AND ((r0.id) <> (r4.id))) AND ((r0.id) <> (r5.id))) AND ((r1.id) <> (r3.id))) AND ((r1.id) <> (r4.id))) AND ((r1.id) <> (r5.id))) AND ((r3.id) <> (r4.id))) AND ((r3.id) <> (r5.id))) AND ((r4.id) <> (r5.id)))
UNWIND [-2082148190, -1254759639, (r1.k62)] AS a0
RETURN (r0.k55) AS a1
ORDER BY a1 DESC;

MATCH (n1 :L3), (n4 :L3), (n0 :L3)<-[r4 :T0]-(n5 :L1)
WHERE ((r4.id) > -1)
UNWIND [(n4.k22)] AS a0
OPTIONAL MATCH (n3)-[]->(n0)<-[]-(n5)
OPTIONAL MATCH (n0 :L3)<-[]-(n1 :L3)<-[]-(n2 :L3), (n6 :L2 :L0)
WHERE ((n4.k18) CONTAINS (n4.k18))
OPTIONAL MATCH (n7 :L4 :L2)-[r5 :T5]->(n1 :L3), (n6 :L2 :L0)
WHERE ((n2.k19) AND (r5.k63))
OPTIONAL MATCH (n6)
WITH n0, (n6.k12) AS a1, max('2Fc') AS a2, r4, a0
WHERE ((n0.k17) AND (n0.k17))
RETURN (r4.k34) AS a3;

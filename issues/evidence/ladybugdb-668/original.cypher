MATCH (n0),
  (n1 :L5 :L3)<-[r0 :T4]-(n2 :L4 :L0)-[r1 :T5]->(n3 :L2)
WHERE (((r0.id) > -1)
  AND ((r0.id) <> (r1.id)))
UNWIND [(r1.k71), (n1.k30), 1151137086] AS a0
OPTIONAL MATCH (n0),
  (n3)-[r2 :T1]->(n4 :L5 :L2)
WHERE ((r2.id) > -1)
OPTIONAL MATCH (n4 :L4 :L2 :L5)<-[]-(n3)<-[]-(n2)
WHERE (1354844104 = 1354844104)
OPTIONAL MATCH (n2)-[]->(n3)-[]->(n4 :L2),
  (n0)
WHERE ((r1.k70)
  AND (r1.k70))
OPTIONAL MATCH (n4)-[r3 :T0]->(n5)
WHERE (r3.k38)
WITH min('5tP') AS a1,
  collect(-401349781) AS a2
UNWIND [-1523432737, 451047109, 451047109] AS a3
RETURN 0 AS a4

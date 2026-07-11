MATCH (n0)<-[r0 :T2]-(n1)<-[r1 :T5]-(n2 :L4), (n3 :L1), (n4)
WHERE ((((r0.id) > -1) AND ((r0.id) <> (r1.id)))) AND NOT(NOT(true OR (coalesce(n0.k9 = n0.k9, true))))
OPTIONAL MATCH (n0 :L1)<-[]-(n1)<-[]-(n2)
WITH min('KeY') AS a0, n4, (n0.k9) AS a1, (r1.k60) AS a2, max('8C') AS a3
RETURN 3 AS a4;

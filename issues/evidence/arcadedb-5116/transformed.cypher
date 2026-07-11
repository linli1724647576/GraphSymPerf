MATCH (n1), (n0), (n0 :L1 :L5)
WHERE ((n0.k9) OR (n0.k8))
OPTIONAL MATCH (n2 :L5)-[r0 :T1]->(n3 :L3 :L0), (n1)
WHERE ('b' ENDS WITH '0ZIeb')
OPTIONAL MATCH (n1)
RETURN max('h') AS a0, (n0.k36) AS a1, (r0.k52) AS a2;

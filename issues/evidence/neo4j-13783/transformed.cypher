MATCH (n0:L5), (n1:L5:L0:L2), (n4)<-[r2:T3]-(n5:L3)
WHERE (r2.id > -1) OR (false AND ((elementId(n0) = elementId(n0)) OR (size(labels(n0)) >= 0)))
MATCH (n4)<-[]-(n5), (n5:L3)-[r3:T3]->(n6:L1)-[r4:T3]->(n7:L0)
WHERE n5.k24 AND n0.k38 AND r3.id <> r4.id
WITH r4
WHERE r4.k60 >= r4.k60
RETURN r4.k56 AS a1;

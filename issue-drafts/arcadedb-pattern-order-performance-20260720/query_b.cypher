WITH 1 AS x
MATCH (a)-[:T1]->(b), (n), (c)-[:T4]->(d)
RETURN count(*) AS c;

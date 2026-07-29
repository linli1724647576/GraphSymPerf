WITH 1 AS x
MATCH (a)-[:T1]->(b), (c)-[:T4]->(d), (n)
RETURN count(*) AS c;

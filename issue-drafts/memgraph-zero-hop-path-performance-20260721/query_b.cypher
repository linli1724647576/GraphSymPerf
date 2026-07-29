MATCH (n:Item)-[:UNUSED*0..0]->(m:Item)
RETURN count(*) AS c;

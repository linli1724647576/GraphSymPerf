MATCH (a:A)-[r:LINK]->(b:B)
RETURN count(r) AS c;

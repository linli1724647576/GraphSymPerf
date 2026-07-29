MATCH (a:A)-[r]->(b:B)
WHERE type(r) = 'LINK'
RETURN count(r) AS c;

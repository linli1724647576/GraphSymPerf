MATCH (n:Item)
WHERE NOT NOT (n.key = 42)
RETURN count(*) AS c;

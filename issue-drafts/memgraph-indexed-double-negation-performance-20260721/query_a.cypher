MATCH (n:Item)
WHERE n.key = 42
RETURN count(*) AS c;

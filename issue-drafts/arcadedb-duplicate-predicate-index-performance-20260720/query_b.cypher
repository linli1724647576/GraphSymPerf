MATCH (n:Bench)
WHERE (n.id = 1) AND (n.id = 1)
RETURN count(*) AS c;

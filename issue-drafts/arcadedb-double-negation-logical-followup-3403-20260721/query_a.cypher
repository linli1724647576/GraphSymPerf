MATCH (n:BoolCase)
WHERE n.flag
RETURN n.id AS id
ORDER BY id;

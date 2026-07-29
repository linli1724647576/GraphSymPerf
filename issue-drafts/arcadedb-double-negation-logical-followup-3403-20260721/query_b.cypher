MATCH (n:BoolCase)
WHERE NOT NOT n.flag
RETURN n.id AS id
ORDER BY id;

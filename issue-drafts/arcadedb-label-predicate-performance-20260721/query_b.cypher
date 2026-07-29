MATCH (n)
WHERE n:Parent
RETURN n.id AS id
ORDER BY id;

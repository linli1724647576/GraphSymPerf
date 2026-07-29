UNWIND range(1, 20000) AS i
CREATE (:Bench {id: i})
RETURN count(*) AS c;

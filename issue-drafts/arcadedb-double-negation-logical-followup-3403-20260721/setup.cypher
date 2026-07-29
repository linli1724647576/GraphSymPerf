CREATE (:BoolCase {id: 1, flag: true}) RETURN count(*) AS c;
CREATE (:BoolCase {id: 2, flag: false}) RETURN count(*) AS c;
CREATE (:BoolCase {id: 3}) RETURN count(*) AS c;

CREATE (:Parent {id: 1}) RETURN count(*) AS c;
CREATE (:Child {id: 2}) RETURN count(*) AS c;

UNWIND range(1, 300000) AS i
CREATE (:Other {id: i})
RETURN count(*) AS c;

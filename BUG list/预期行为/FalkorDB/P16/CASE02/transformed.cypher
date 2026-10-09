RETURN all(x IN (range(1, 40000) + [0]) WHERE coalesce(CASE WHEN (x = x) THEN (x <> 0) ELSE null END, null)) AS ok;

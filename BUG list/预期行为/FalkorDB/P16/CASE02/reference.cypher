RETURN all(x IN ([0] + []) WHERE coalesce(CASE WHEN (x = x) THEN (x <> 0) ELSE null END, null)) AS ok;

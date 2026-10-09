RETURN all(x IN (range(40000, 1, -1) + [0]) WHERE CASE WHEN (x = x) THEN coalesce((x <> 0), null) ELSE null END) AS ok;

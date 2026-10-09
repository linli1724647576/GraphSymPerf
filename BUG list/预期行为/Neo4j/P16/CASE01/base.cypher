RETURN all(x IN ([0] + range(40000, 1, -1)) WHERE CASE WHEN (x = x) THEN coalesce((x <> 0), null) ELSE null END) AS ok;

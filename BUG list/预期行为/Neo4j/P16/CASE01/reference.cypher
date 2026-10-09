RETURN all(x IN ([0] + []) WHERE CASE WHEN (x = x) THEN coalesce((x <> 0), null) ELSE null END) AS ok;

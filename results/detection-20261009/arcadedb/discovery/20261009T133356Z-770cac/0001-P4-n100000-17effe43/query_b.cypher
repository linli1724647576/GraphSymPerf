MATCH (u:GspAnchor) WHERE ((COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0) AND (coalesce(u.keep, false) = true)) RETURN u.uid AS c0;

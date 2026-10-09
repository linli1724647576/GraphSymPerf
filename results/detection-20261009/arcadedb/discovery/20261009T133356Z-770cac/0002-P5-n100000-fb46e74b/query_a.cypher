MATCH (u:GspAnchor) WHERE ((coalesce(u.keep, false) = true) OR (COUNT { MATCH (u)-[:GSP_EXPAND]->(gsp_leaf:GspLeaf) WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0)) RETURN u.uid AS c0;

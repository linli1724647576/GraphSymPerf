MATCH (u:GspAnchor) WHERE ((coalesce(u.keep, false) = true) AND (COUNT { MATCH (u)-[:GSP_REFERENCE]->(gsp_leaf:GspLeaf) WHERE (coalesce(gsp_leaf.mark, false) = true) } > 0)) RETURN u.uid AS c0;

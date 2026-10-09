MATCH (u:ProbeRoot) RETURN u.uid AS uid, COUNT { MATCH (x:ProbeLeaf) WHERE x.key=u.key AND x.mark=true } AS value;

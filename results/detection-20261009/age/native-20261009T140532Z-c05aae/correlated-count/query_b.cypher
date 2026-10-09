MATCH (u:ProbeRoot) OPTIONAL MATCH (x:ProbeLeaf) WHERE x.key=u.key AND x.mark=true WITH u,count(x) AS value RETURN u.uid AS uid,value;

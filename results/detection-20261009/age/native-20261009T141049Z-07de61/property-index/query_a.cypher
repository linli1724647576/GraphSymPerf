MATCH (n:ProbeNode) WHERE coalesce(n.id,null)=1 RETURN n.uid AS uid;

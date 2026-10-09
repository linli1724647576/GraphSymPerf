MATCH (b:GspHigh) WHERE (NOT (b.keep = false)) RETURN b.uid AS uid UNION ALL MATCH (a:GspLow) RETURN a.uid AS uid;

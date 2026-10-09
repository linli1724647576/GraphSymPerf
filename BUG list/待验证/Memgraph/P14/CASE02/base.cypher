MATCH (a:GspLow) RETURN a.uid AS uid UNION ALL MATCH (b:GspHigh) WHERE (NOT (b.keep = false)) RETURN b.uid AS uid;

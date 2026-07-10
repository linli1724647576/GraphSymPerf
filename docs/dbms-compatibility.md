# DBMS compatibility

AGE uses an AgensGraph generation proxy and reports that boundary in its manifest. AgensGraph is single-label; multi-label queries are rejected, never reduced to a first label. Unsupported dialect constructs are REJECT, not lossy rewrites.

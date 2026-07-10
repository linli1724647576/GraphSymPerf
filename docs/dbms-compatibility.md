# DBMS compatibility

AGE uses an isolated AgensGraph GDsmith generation proxy (`GRAPHSYMPERF_GDSMITH_PROXY_APACHE_AGE_URI`) and LadybugDB uses an isolated Neo4j proxy (`GRAPHSYMPERF_GDSMITH_PROXY_LADYBUGDB_URI`). Both are hard doctor requirements and are recorded as proxy boundaries in the resolved manifest. AgensGraph is single-label; multi-label queries are rejected, never reduced to a first label. Unsupported dialect constructs are REJECT, not lossy rewrites.

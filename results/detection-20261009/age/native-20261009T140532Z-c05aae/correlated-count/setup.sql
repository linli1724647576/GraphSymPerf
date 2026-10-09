LOAD 'age';

SET search_path = ag_catalog, "$user", public;

DEALLOCATE ALL;

SELECT set_config('statement_timeout', '60000', false);

SELECT ag_catalog.drop_graph('gsp_cases',true);

SELECT ag_catalog.create_graph('gsp_cases');

SELECT ag_catalog.create_vlabel('gsp_cases','ProbeRoot');

SELECT ag_catalog.create_vlabel('gsp_cases','ProbeLeaf');

INSERT INTO "gsp_cases"."ProbeRoot" (properties) SELECT (jsonb_build_object('uid',i,'key',1)::text)::ag_catalog.agtype FROM generate_series(1,16) AS s(i);

INSERT INTO "gsp_cases"."ProbeLeaf" (properties) SELECT (jsonb_build_object('uid',i,'key',1,'mark',false)::text)::ag_catalog.agtype FROM generate_series(1,100000) AS s(i);

INSERT INTO "gsp_cases"."ProbeLeaf" (properties) VALUES ((jsonb_build_object('uid',100001,'key',1,'mark',true)::text)::ag_catalog.agtype);

CREATE INDEX "probe_cost_lookup" ON "gsp_cases"."ProbeRoot" USING gin (properties);

CREATE INDEX "probe_cost_lookup_scalar" ON "gsp_cases"."ProbeRoot" ((ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties,'"uid"'::ag_catalog.agtype])));

VACUUM (ANALYZE) "gsp_cases"."ProbeRoot";

VACUUM (ANALYZE) "gsp_cases"."ProbeLeaf";

DISCARD PLANS;

LOAD 'age';

SET search_path = ag_catalog, "$user", public;

DEALLOCATE ALL;

SELECT set_config('statement_timeout', '60000', false);

SELECT ag_catalog.create_graph('gsp_cases');

SELECT ag_catalog.create_vlabel('gsp_cases','ProbeNode');

INSERT INTO "gsp_cases"."ProbeNode" (properties) SELECT (jsonb_build_object('uid',i,'id',i)::text)::ag_catalog.agtype FROM generate_series(1,500000) AS s(i);

CREATE INDEX "probe_cost_lookup" ON "gsp_cases"."ProbeNode" USING gin (properties);

CREATE INDEX "probe_cost_lookup_scalar" ON "gsp_cases"."ProbeNode" ((ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties,'"id"'::ag_catalog.agtype])));

VACUUM (ANALYZE) "gsp_cases"."ProbeNode";

DISCARD PLANS;

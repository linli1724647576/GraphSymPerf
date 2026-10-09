import sys,json,time
from pathlib import Path
ROOT=Path('/Users/sakuraq/Documents/科研实习/GraphSymPerf')
sys.path.insert(0,str(ROOT/'Src'))
from main import read_config,read_case
from executor import make_database,typed_bag
c=read_config(Path(sys.argv[1])); source=Path(sys.argv[2]); case,_=read_case(source)
db=make_database(c['database'],c['measurement'])
out={}
try:
    db.restore(case)
    out['statistics']=db._execute('ANALYZE GRAPH',{},60)
    if case['pattern']=='P10':
        queries={
          'single_match_low_first':'MATCH (a:GspLow), (b:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE a.id=1 AND coalesce(c.keep,false)=true RETURN a.uid,b.uid,c.uid',
          'single_match_high_first':'MATCH (b:GspHigh)-[:GSP_LINK]->(c:GspTarget), (a:GspLow) WHERE a.id=1 AND coalesce(c.keep,false)=true RETURN a.uid,b.uid,c.uid'}
    else:
        queries={
          'single_match_high_first':'MATCH (u:GspAnchor {uid:1})-[:GSP_HIGH]->(w:GspHigh), (u)-[:GSP_LOW]->(v:GspLow) WHERE NOT (w.keep=false) RETURN u.uid,v.uid,w.uid',
          'single_match_low_first':'MATCH (u:GspAnchor {uid:1})-[:GSP_LOW]->(v:GspLow), (u)-[:GSP_HIGH]->(w:GspHigh) WHERE NOT (w.keep=false) RETURN u.uid,v.uid,w.uid'}
    for name,query in {**{k:case[k] for k in ['query_a','query_b']},**queries}.items():
        results=[db._execute(query,{},60) for _ in range(3)]
        assert all(r['complete'] and typed_bag(r['rows'])==typed_bag(case['expected_rows']) for r in results)
        out[name]={'query':query,'results':results,'profile':db.explain(query,{},analyze=True)}
finally:
    db.close()
destination=ROOT/'results/detection-20261009/memgraph'/('controls-'+case['pattern']+'.json')
destination.write_text(json.dumps(out,indent=2)+'\n')
print(destination,flush=True)

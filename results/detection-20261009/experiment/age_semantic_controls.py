import sys,json,os
from pathlib import Path
ROOT=Path('/Users/sakuraq/Documents/科研实习/GraphSymPerf')
sys.path.insert(0,str(ROOT/'Src'))
from main import read_config
from executor import make_database,typed_bag
base=Path('/private/tmp/gsp-detection-20261009')
os.environ.update(json.loads((base/'credentials.json').read_text()))
c=read_config(base/'age.yaml'); c['database']['graph']='gsp_semantic_controls'
db=make_database(c['database'],c['measurement']); sql=db._sql_module
out={}
try:
    if db._sql('SELECT name FROM ag_catalog.ag_graph WHERE name=%s',(db.graph,)):
        db._sql('SELECT ag_catalog.drop_graph(%s,true)',(db.graph,))
    db._sql('SELECT ag_catalog.create_graph(%s)',(db.graph,))
    fixtures={
      'ProbeNode':[{'uid':1,'id':1},{'uid':2,'id':2},{'uid':3,'id':None},{'uid':4}],
      'ProbeRoot':[{'uid':1,'key':1},{'uid':1,'key':1},{'uid':3,'key':2},{'uid':4,'key':None}],
      'ProbeLeaf':[{'uid':1,'key':1,'mark':True},{'uid':2,'key':1,'mark':False}]}
    for label,rows in fixtures.items():
        db._sql('SELECT ag_catalog.create_vlabel(%s,%s)',(db.graph,label))
        for row in rows:
            db._sql(sql.SQL('INSERT INTO {}.{}(properties) VALUES(%s::ag_catalog.agtype)').format(sql.Identifier(db.graph),sql.Identifier(label)),(json.dumps(row),))
    pairs={
      'coalesce-index':(
        'MATCH(n:ProbeNode) WHERE coalesce(n.id,null)=1 RETURN n.uid AS uid',
        'MATCH(n:ProbeNode {id:1}) RETURN n.uid AS uid',
        ('uid',),[{'uid':1}]),
      'correlated-count':(
        'MATCH(u:ProbeRoot) RETURN u.uid AS uid,COUNT { MATCH(x:ProbeLeaf) WHERE x.key=u.key AND x.mark=true } AS value',
        'MATCH(u:ProbeRoot) OPTIONAL MATCH(x:ProbeLeaf) WHERE x.key=u.key AND x.mark=true WITH u,count(x) AS value RETURN u.uid AS uid,value',
        ('uid','value'),[{'uid':1,'value':1},{'uid':1,'value':1},{'uid':3,'value':0},{'uid':4,'value':0}])}
    for name,(a,b,columns,expected) in pairs.items():
        evidence={}
        for side,query in [('a',a),('b',b)]:
            db._columns[query]=columns
            result=db.execute(query,{},60)
            assert result['complete'] and typed_bag(result['rows'])==typed_bag(expected), (name,side,result)
            evidence[side]={'query':query,'result':result}
        out[name]=evidence
    out['fixtures']=fixtures
    out['version']=db.version
finally:
    db._sql('SELECT ag_catalog.drop_graph(%s,true)',(db.graph,))
    db.close()
p=ROOT/'results/detection-20261009/age/semantic-controls.json'
p.write_text(json.dumps(out,indent=2)+'\n')
print('AGE missing/NULL properties, duplicate root uid and empty matches passed',flush=True)

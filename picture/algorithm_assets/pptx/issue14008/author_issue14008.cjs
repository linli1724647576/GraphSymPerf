// Native PowerPoint figure specifications for the real Neo4j #14008 example.
// Existing SVG illustrations are intentionally not changed by this authoring source.
const {Canvas}=require('skia-canvas');
const fs=require('fs'),path=require('path');
const context=new Canvas(8,8).getContext('2d');
const BLACK='#000000',WHITE='#FFFFFF',RED='#B32025';
class Fig {
 constructor(name,h,title){this.name=name;this.width=1000;this.height=h;this.widthMm=180;this.heightMm=.18*h;this.title=title;this.source='https://github.com/neo4j/neo4j/issues/14008';this.elements=[];this.groupName='canvas';this.rect(0,0,1000,h,{stroke:'none'});}
 group(g){this.groupName=g;}
 add(type,attrs,extra={}){const index=this.elements.length;this.elements.push({type,attrs,index,group:this.groupName,name:`${this.groupName}_${type}_${index}`,...extra});}
 rect(x,y,w,h,o={}){this.add('rect',{x,y,width:w,height:h,rx:o.r||0,fill:o.fill||WHITE,stroke:o.stroke||BLACK,'stroke-width':o.sw||.8,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 circle(x,y,r=16,o={}){this.add('circle',{cx:x,cy:y,r,fill:WHITE,stroke:o.color||BLACK,'stroke-width':o.sw||1.25,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 line(x1,y1,x2,y2,o={}){this.add('line',{x1,y1,x2,y2,stroke:o.color||BLACK,'stroke-width':o.sw||1.1,...(o.arrow?{'marker-end':'url(#triangle)'}:{})});}
 text(x,y,value,size=16,o={}){
  const font=o.font||'Arial',bold=!!o.bold,italic=!!o.italic,color=o.color||BLACK;
  const pieces=value.replace(/\s+/g,' ').split(/(Q_[12]|↦)/).filter(Boolean), runs=[];
  function measure(t,face,sz){context.font=`${italic?'italic ':''}${bold?'bold ':''}${sz}px "${face}"`;return context.measureText(t);}
  for(const piece of pieces){
   if(/^Q_[12]$/.test(piece)){for(const [t,sz,dy]of [['Q',size,0],[piece.slice(-1),size*.72,size*.432331]]){const m=measure(t,font,sz);runs.push({text:t,font,size:sz,bold,italic,color,dy,advance:m.width,m});}}
   else {const face=piece==='↦'?'Menlo':font,m=measure(piece,face,size);runs.push({text:piece,font:face,size,bold,italic,color,dy:0,advance:m.width,m,fallback:piece==='↦'});}
  }
  const width=runs.reduce((a,r)=>a+r.advance,0);let left=x-(o.anchor==='middle'?width/2:o.anchor==='end'?width:0),loX=Infinity,loY=Infinity,hiX=-Infinity,hiY=-Infinity;
  for(const r of runs){r.x=left;r.y=y+r.dy;left+=r.advance;loX=Math.min(loX,r.x-r.m.actualBoundingBoxLeft);loY=Math.min(loY,r.y-r.m.actualBoundingBoxAscent);hiX=Math.max(hiX,r.x+r.m.actualBoundingBoxRight);hiY=Math.max(hiY,r.y+r.m.actualBoundingBoxDescent);delete r.m;delete r.dy;}
  this.add('text',{x,y,fill:color,'font-family':font,'font-size':size,'font-weight':bold?700:400,'font-style':italic?'italic':'normal','text-anchor':o.anchor||'start'},
    {text:value.replace(/Q_([12])/g,'Q$1'),runs,bbox:{x:loX,y:loY,w:hiX-loX,h:hiY-loY}});
 }
 math(x,y,t,size=19,o={}){this.text(x,y,t,size,{font:'Times New Roman',italic:true,...o});}
 code(x,y,t,size=15,o={}){this.text(x,y,t,size,{font:'Arial',...o});}
 frame(x,w,label,h=250){this.rect(x,8,w,h);this.text(x+w/2,32,label,17,{bold:true,anchor:'middle'});this.line(x,46,x+w,46,{sw:.7});}
 arrow(x1,x2,y,lines=[],under){this.line(x1,y,x2,y,{sw:1.35,arrow:true});lines.forEach((t,i)=>this.text((x1+x2)/2,y-16-(lines.length-i-1)*17,t,14.5,{anchor:'middle'}));if(under)this.math((x1+x2)/2,y+25,under,18,{anchor:'middle'});}
}
const f1=new Fig('Algorithm_1_graph_state_generation',270,'Algorithm 1: graph state construction for Neo4j #14008');
f1.group('p3-skeleton');f1.frame(8,254,'(a) Skeleton S');
f1.text(135,68,'P3 · bounded-path symmetry',14.5,{bold:true,anchor:'middle'});
f1.code(135,92,'MATCH p =',14.5,{anchor:'middle'});
f1.line(82,137,184,137);f1.circle(65,137,17,{dash:'3 3'});f1.circle(202,137,18,{dash:'3 3'});
f1.math(65,143,'L',20,{anchor:'middle'});f1.math(202,143,'H',20,{anchor:'middle'});
f1.code(135,121,'T*m..n',14.5,{anchor:'middle'});
f1.code(135,182,'WHERE φ(p)',15,{anchor:'middle'});
f1.code(135,218,'pruning_path',14.5,{anchor:'middle'});
f1.math(135,242,'N = 300,000',17,{anchor:'middle'});
f1.arrow(269,295,143);
f1.group('p3-graph');f1.frame(302,414,'(b) Graph state G');
f1.code(509,69,'All edges: GSP_LINK',14.5,{anchor:'middle'});
// Stored relationships are directed; query patterns in Algorithm 2 are undirected.
f1.line(358,98,486,98,{arrow:true});f1.line(514,98,646,98,{arrow:true});
f1.line(358,104,423,141);f1.line(423,141,418,164,{arrow:true});f1.line(424,176,521,176,{arrow:true});f1.line(549,176,646,176,{arrow:true});
for(const [x,y,t,col]of [[344,98,'1',BLACK],[500,98,'2',BLACK],[660,98,'3',BLACK],[410,176,'0',RED]]){f1.circle(x,y,14.5,{color:col});f1.text(x,y+5,t,17,{anchor:'middle',color:col});}
f1.circle(535,176,14);f1.circle(660,176,14);
f1.code(344,128,':GspLow',14.5,{anchor:'middle'});f1.code(344,150,'{id:1}',14.5,{anchor:'middle'});f1.code(500,128,':GspMiddle',14.5,{anchor:'middle'});f1.code(660,128,':GspHigh',14.5,{anchor:'middle'});
f1.code(410,205,':GspBad',14.5,{anchor:'middle'});f1.code(535,205,':GspMiddle',14.5,{anchor:'middle'});f1.code(660,205,':GspHigh',14.5,{anchor:'middle'});
// Open bracket marks a repeated two-hop motif; it does not add graph edges.
f1.line(514,152,514,145,{sw:.8});f1.line(514,145,683,145,{sw:.8});f1.line(683,145,683,152,{sw:.8});
f1.math(599,139,'× N',17,{anchor:'middle'});
f1.code(410,229,'keep=false',14.5,{anchor:'middle',color:RED});
f1.text(530,249,'Other nodes: keep=true',14.5,{anchor:'middle'});
f1.arrow(723,749,143);
f1.group('p3-output');f1.frame(756,236,'(c) Output');
f1.math(874,82,'(G, I)',27,{anchor:'middle'});
f1.math(780,112,'I:',18);f1.text(808,112,'unique anchor u',14.5);
f1.code(808,139,'uid 0: keep=false',14.5);f1.text(808,166,'2-hop witness',14.5);
f1.code(874,194,'index: GspLow(id)',14.5,{anchor:'middle'});
f1.line(874,207,874,224,{arrow:true,sw:1.3});f1.text(874,248,'Algorithm 2',16.5,{anchor:'middle'});
f1.notes='Real running example: Neo4j issue #14008, https://github.com/neo4j/neo4j/issues/14008. Pattern P3, SY. N=300000; state_variant=pruning_path; data seed=0. Numbers inside nodes are uid values. The bracket repeats the two-hop GspMiddle-to-GspHigh motif N times, each connected to the same GspBad uid=0. All stored edges are directed GSP_LINK; all nodes except uid=0 have keep=true. Root uid=1 has id=1 and a GspLow(id) index. The valid witness is uid 1->2->3. There are 600004 nodes and 600003 relationships. G is a construction specification; conditions I and index readiness are verified after materialization. No reference region R is used for P3.';
const f2=new Fig('Algorithm_2_query_pair_construction',454,'Algorithm 2: the real query pair from Neo4j #14008');
for(const [x,label]of [[8,'Skeleton S'],[378,'Base query Q_1'],[748,'Mutated query Q_2']]){f2.frame(x,244,label,438);f2.line(x,263,x+244,263,{sw:.7});}
f2.group('p3-handoff');f2.math(315,36,'(G, I)',19,{anchor:'middle'});f2.line(315,50,315,91,{arrow:true});
f2.group('p3-query-pair');f2.text(20,70,'P3 · Neo4j #14008',15.5,{bold:true});
for(const [cx,mode]of [[130,'skeleton'],[500,'base'],[870,'mutated']]){
 const left=cx-57,right=cx+57;f2.code(cx,98,'MATCH p =',14.5,{anchor:'middle'});
 f2.line(left+17,155,right-17,155);f2.code(cx,128,mode==='skeleton'?'T*m..n':'GSP_LINK*1..3',14.5,{anchor:'middle'});
 const names=mode==='skeleton'?['L','H']:mode==='base'?['u','v']:['v','u'];
 for(const [i,x]of [left,right].entries()){const color=mode==='skeleton'?BLACK:RED;f2.circle(x,155,17,{color,...(mode==='skeleton'?{dash:'3 3'}:{})});f2.math(x,161,names[i],20,{anchor:'middle',color});
  if(mode!=='skeleton'){const isU=names[i]==='u';f2.code(x,193,isU?':GspLow':':GspHigh',14.5,{anchor:'middle'});if(isU)f2.code(x,215,'{id:1}',14.5,{anchor:'middle'});}
 }
 if(mode==='skeleton')f2.code(cx,215,'WHERE φ(p)',15,{anchor:'middle'});
}
f2.arrow(265,365,155,['Skeleton','population'],'β');f2.arrow(635,735,155,['Symmetry','transformation']);
f2.text(685,189,'P3',14.5,{anchor:'middle'});
f2.group('p3-bindings');f2.text(130,288,'Substitution map β',16,{bold:true,anchor:'middle'});
// Pictorial binding: a placeholder is filled by a labelled node pattern,
// not by one concrete result node. H still denotes every GspHigh candidate.
for(const [cy,role,variable,label]of [[320,'L','u',':GspLow'],[377,'H','v',':GspHigh']]){
 f2.circle(43,cy,14,{dash:'3 3'});f2.math(43,cy+6,role,19,{anchor:'middle'});
 f2.line(60,cy,94,cy,{arrow:true,sw:1.2});
 f2.circle(111,cy,14,{color:RED});f2.math(111,cy+6,variable,19,{anchor:'middle',color:RED});
 f2.text(135,cy+4,label,15);
 if(variable==='u')f2.text(135,cy+25,'{id:1}',15);
}
f2.text(130,436,'Neo4j 2026.09.0',14.5,{anchor:'middle'});
f2.group('p3-witness-and-result');
for(const [cx,reverse,time]of [[500,false,'2.70 ms'],[870,true,'530.10 ms']]){
 f2.text(cx,283,'Matching witness p',15.5,{bold:true,anchor:'middle'});
 const xs=[cx-72,cx,cx+72], ids=reverse?[3,2,1]:[1,2,3];
 // Undirected witness order follows query syntax, not physical expansion order.
 f2.line(xs[0]+12,314,xs[1]-12,314);f2.line(xs[1]+12,314,xs[2]-12,314);
 for(let i=0;i<3;i++){
  const x=xs[i];f2.circle(x,314,12);f2.text(x,319,String(ids[i]),15.5,{anchor:'middle'});
  // Native tick: the node satisfies keep=true. No symbol font is required.
  f2.line(x+11,305,x+14,308,{sw:1.15});f2.line(x+14,308,x+20,299,{sw:1.15});
  if(ids[i]===1||ids[i]===3)f2.math(x,340,ids[i]===1?'u':'v',17,{anchor:'middle'});
 }
 f2.text(cx,357,'φ(p): all nodes keep=true',14.5,{anchor:'middle'});
 f2.line(cx,364,cx,371,{arrow:true,sw:1.1});
 // Editable result table is a native shape composite: projection names + one row.
 const left=cx-90,top=376;f2.rect(left,top,180,40,{sw:.8});
 f2.line(left,top+20,left+180,top+20,{sw:.7});
 for(const x of [left+60,left+120])f2.line(x,top,x,top+40,{sw:.7});
 ['u.uid','v.uid','hops'].forEach((t,i)=>f2.text(left+30+i*60,top+14,t,14.5,{anchor:'middle'}));
 [1,3,2].forEach((v,i)=>f2.text(left+30+i*60,top+35,String(v),15.5,{anchor:'middle'}));
 f2.text(cx,438,'Runtime: '+time,16,{anchor:'middle'});
}
f2.notes='Source: https://github.com/neo4j/neo4j/issues/14008. This single P3 symmetry instance replaces the previous S7/P1 schematic examples. Q2 is derived from a copy of Q1 by mirroring the written undirected bounded path; the predicate and projection are unchanged. Both plans actually start at indexed u; the drawing shows query syntax, not physical traversal order. The graphical witnesses depict accepted paths, not physical expansion order; both plans start from indexed u. Black ticks mean keep=true on the witness node. The result table fixes the projection order u.uid, v.uid, hops despite reversed path order. The top patterns carry the relationship-type and path-bound substitutions; the lower left illustrates the L/H bindings. Runtime labels display the reported ordinary-runtime medians (2.70 ms and 530.10 ms) and common result (1,3,2) are reported for Neo4j 2026.09.0 / Cypher 25, not measurements rerun during this figure edit. The local generator with N=300000, seed=0, data_seed=0, complexity=0, state_variant=pruning_path produces this pair. This issue does not instantiate the short-circuit reference-substitution branch and uses no R region. Current GitHub API status: closed/completed 2026-10-15; contributor comment https://github.com/neo4j/neo4j/issues/14008#issuecomment-6190652273 says it should be fixed in 2026.10; that version was not independently tested here.';
fs.writeFileSync(path.join(__dirname,'issue_geometry.json'),JSON.stringify([f1,f2],null,2)+'\n');
console.log('Authored native P3 figures for both algorithms.');

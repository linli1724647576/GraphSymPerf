// Native editable diagram of Algorithm 1 only: graph construction specification and conditions.
// Existing SVG illustrations are intentionally not changed by this authoring source.
const {Canvas}=require('skia-canvas');
const fs=require('fs'),path=require('path');
const context=new Canvas(8,8).getContext('2d');
const BLACK='#000000',WHITE='#FFFFFF',RED='#B32025';
class Fig {
 constructor(name,h,title){this.name=name;this.width=1000;this.height=h;this.widthMm=180;this.heightMm=.18*h;this.title=title;this.source='docs/Approach.md: Algorithm 1';this.elements=[];this.groupName='canvas';this.rect(0,0,1000,h,{stroke:'none'});}
 group(g){this.groupName=g;}
 add(type,attrs,extra={}){const index=this.elements.length;this.elements.push({type,attrs,index,group:this.groupName,name:`${this.groupName}_${type}_${index}`,...extra});}
 rect(x,y,w,h,o={}){this.add('rect',{x,y,width:w,height:h,rx:o.r||0,fill:o.fill||WHITE,stroke:o.stroke||BLACK,'stroke-width':o.sw||.8,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 circle(x,y,r=16,o={}){this.add('circle',{cx:x,cy:y,r,fill:WHITE,stroke:o.color||BLACK,'stroke-width':o.sw||1.25,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 line(x1,y1,x2,y2,o={}){this.add('line',{x1,y1,x2,y2,stroke:o.color||BLACK,'stroke-width':o.sw||1.1,...(o.arrow?{'marker-end':'url(#triangle)'}:{})});}
 text(x,y,value,size=16,o={}){
  const font=o.font||'Arial',bold=!!o.bold,italic=!!o.italic,color=o.color||BLACK;
  const pieces=value.replace(/\s+/g,' ').split(/(Q_[12]|V_x|E_[HR]|k_G|n_[UF]|T_[fr]|↦)/).filter(Boolean), runs=[];
  function measure(t,face,sz){context.font=`${italic?'italic ':''}${bold?'bold ':''}${sz}px "${face}"`;return context.measureText(t);}
  for(const piece of pieces){
   if(/^(Q_[12]|V_x|E_[HR]|k_G|n_[UF]|T_[fr])$/.test(piece)){for(const [t,sz,dy]of [[piece[0],size,0],[piece.slice(-1),size*.72,size*.22]]){const m=measure(t,font,sz);runs.push({text:t,font,size:sz,bold,italic,color,dy,advance:m.width,m});}}
   else {const face=piece==='↦'?'Menlo':font,m=measure(piece,face,size);runs.push({text:piece,font:face,size,bold,italic,color,dy:0,advance:m.width,m,fallback:piece==='↦'});}
  }
  const width=runs.reduce((a,r)=>a+r.advance,0);let left=x-(o.anchor==='middle'?width/2:o.anchor==='end'?width:0),loX=Infinity,loY=Infinity,hiX=-Infinity,hiY=-Infinity;
  for(const r of runs){r.x=left;r.y=y+r.dy;left+=r.advance;loX=Math.min(loX,r.x-r.m.actualBoundingBoxLeft);loY=Math.min(loY,r.y-r.m.actualBoundingBoxAscent);hiX=Math.max(hiX,r.x+r.m.actualBoundingBoxRight);hiY=Math.max(hiY,r.y+r.m.actualBoundingBoxDescent);delete r.m;delete r.dy;}
  this.add('text',{x,y,fill:color,'font-family':font,'font-size':size,'font-weight':bold?700:400,'font-style':italic?'italic':'normal','text-anchor':o.anchor||'start'},
    {text:value.replace(/([QVEknT])_([12xHRGUFrf])/g,'$1$2'),runs,bbox:{x:loX,y:loY,w:hiX-loX,h:hiY-loY}});
 }
 math(x,y,t,size=19,o={}){this.text(x,y,t,size,{font:'Times New Roman',italic:true,...o});}
 code(x,y,t,size=15,o={}){this.text(x,y,t,size,{font:'Arial',...o});}
 frame(x,w,label,h=250){this.rect(x,8,w,h);this.text(x+w/2,32,label,17,{bold:true,anchor:'middle'});this.line(x,46,x+w,46,{sw:.7});}
 arrow(x1,x2,y,lines=[],under){this.line(x1,y,x2,y,{sw:1.35,arrow:true});lines.forEach((t,i)=>this.text((x1+x2)/2,y-16-(lines.length-i-1)*17,t,14.5,{anchor:'middle'}));if(under)this.math((x1+x2)/2,y+25,under,18,{anchor:'middle'});}
}
const f=new Fig('Algorithm_1_graph_state_generation',622,'Algorithm 1: graph state generation');
f.group('header');f.text(15,30,'Graph state generation',19,{bold:true});
f.text(715,30,'Input:',15);f.math(975,30,'S, p, N, v, k_G',20,{anchor:'end'});
function panel(x,w,title){f.rect(x,46,w,500);f.text(x+15,72,title,18,{bold:true});f.line(x,86,x+w,86,{sw:.7});}
panel(8,220,'(a) Recipe');panel(260,456,'(b) Graph construction');panel(748,244,'(c) Specification');
f.group('recipe');
// A generic skeleton carries cost roles, not an already populated query.
f.math(118,119,'S',25,{anchor:'middle'});f.line(110,125,77,151);f.line(126,125,158,151);
for(const [x,role]of [[72,'L'],[164,'H']]){f.circle(x,166,17,{dash:'3 3'});f.math(x,173,role,21,{anchor:'middle'});}
f.math(118,208,'p, v',19,{anchor:'middle'});f.line(118,216,118,239,{arrow:true});
// Stacked recipe cards, with the selected recipe in front.
f.rect(49,248,149,147,{sw:.7});f.rect(41,256,149,147,{sw:.7});f.rect(33,264,149,147,{sw:1,stroke:RED});
f.math(108,295,'ρ',27,{anchor:'middle',color:RED});
f.text(108,325,'Counts',15.5,{anchor:'middle'});f.text(108,351,'Endpoints',15.5,{anchor:'middle'});f.text(108,377,'Labels · facts',15.5,{anchor:'middle'});
f.text(118,447,'Applicable recipe',15.5,{anchor:'middle'});f.math(118,477,'N > 0',20,{anchor:'middle'});f.text(118,519,'Else: reject',15,{anchor:'middle'});
f.arrow(231,257,268);
f.group('base-groups');
f.text(488,111,'L/H components',16,{bold:true,anchor:'middle'});
// Group boundaries are dashed; solid arrows are schematic graph relationships.
f.rect(278,127,111,121,{dash:'3 3',sw:.8});f.math(333,149,'L',21,{anchor:'middle'});
f.rect(462,127,235,121,{dash:'3 3',sw:.8});f.math(578,149,'H',21,{anchor:'middle'});
f.line(346,181,479,181,{arrow:true});
for(const y of [162,191,222])f.line(503,181,641,y,{arrow:true});
f.circle(333,181,13);f.math(333,187,'u',18,{anchor:'middle'});f.text(333,218,'fixed key',14.5,{anchor:'middle'});
f.circle(491,181,12);for(const y of [162,191,222])f.circle(653,y,11);
f.text(682,198,'⋮',20,{anchor:'middle'});
f.math(578,269,'n_U(N), n_F(N)',18,{anchor:'middle'});
f.text(333,269,'Fixed size',14.5,{anchor:'middle'});
// Property facts are assigned by the recipe, not discovered by executing checks.
f.text(488,296,'Labels · properties · witnesses',14.5,{anchor:'middle'});
f.group('optional');f.line(272,310,704,310,{sw:.7});f.text(488,334,'Optional additions',16,{bold:true,anchor:'middle'});
f.rect(278,348,195,117,{sw:.8});f.rect(487,348,210,117,{sw:.8});
f.text(375,369,'Inverse pairing',15.5,{anchor:'middle'});f.text(592,369,'Reference region',15.5,{anchor:'middle'});
// The inverse-pair sketch denotes edge construction, not query rewriting.
f.line(313,401,438,401,{arrow:true});f.line(438,419,313,419,{arrow:true});f.circle(301,410,14);f.circle(450,410,14);f.math(301,416,'u',18,{anchor:'middle'});f.math(450,416,'v',18,{anchor:'middle'});
f.text(375,395,'FWD',14,{anchor:'middle'});f.text(375,440,'REV',14,{anchor:'middle'});f.text(375,458,'shared pair key',13.5,{anchor:'middle'});
// H and R share the designated anchor. H's ellipsis denotes a scalable population.
f.circle(512,416,11);f.math(512,421,'u',16,{anchor:'middle'});
f.rect(611,378,75,38,{dash:'3 3',sw:.75});f.rect(611,423,75,39,{dash:'3 3',stroke:RED,sw:.8});
for(const y of [385,397,409])f.line(523,411,644,y,{arrow:true});for(const y of [431,443,455])f.line(523,422,644,y,{arrow:true,color:RED});
f.math(556,392,'H',17,{anchor:'middle'});f.math(556,452,'R',17,{anchor:'middle',color:RED});
for(const y of [385,397,409])f.circle(650,y,4.5);for(const y of [431,443,455])f.circle(650,y,4.5,{color:RED});f.text(674,403,'…',18,{anchor:'middle'});
f.text(488,483,'If required',14.5,{anchor:'middle'});
// Identifiers are assigned after group/role structure is fixed.
f.line(272,493,704,493,{sw:.7});f.text(280,521,'Identifiers:',15.5);f.math(392,521,'k_G',18,{anchor:'middle'});f.line(413,515,449,515,{arrow:true});f.text(458,521,'seeded offset',14.5);f.line(558,505,558,531,{sw:.6});f.text(630,521,'fixed keys',14.5,{anchor:'middle'});
f.arrow(719,745,268);
f.group('graph-specification');
f.rect(764,105,212,181,{sw:.9});f.math(870,134,'G',27,{anchor:'middle'});f.text(870,156,'Graph specification',15.5,{anchor:'middle'});
// Native miniature group graph, with schema/roles/index metadata below.
f.line(799,188,842,188,{arrow:true});for(const y of [170,188,208])f.line(860,188,935,y,{arrow:true});
for(const [x,y]of [[790,188],[851,188],[944,170],[944,188],[944,208]])f.circle(x,y,8);
f.text(870,238,'Schema · roles · indexes',14.5,{anchor:'middle'});f.math(870,269,'ρ, p, N, v, k_G',19,{anchor:'middle'});
f.group('conditions');f.line(870,287,870,302,{arrow:true});f.rect(764,305,212,224,{sw:.9});
f.math(870,334,'I',27,{anchor:'middle'});f.text(870,356,'Expected conditions',15.5,{anchor:'middle'});
f.text(870,389,'Cardinalities',14.5,{anchor:'middle'});
f.text(870,420,'Unique identifiers',15,{anchor:'middle'});
f.text(870,450,'Properties · witnesses',15,{anchor:'middle'});
f.text(870,482,'Inverse / reference',15,{anchor:'middle'});f.text(870,507,'constraints',15,{anchor:'middle'});
f.group('handoff');f.line(870,547,870,567);f.line(870,567,502,567);f.line(502,567,502,580,{arrow:true});
f.rect(8,584,984,30,{sw:.9});f.text(28,606,'Output',16,{bold:true});f.math(319,607,'(G, I)',23,{anchor:'middle'});f.line(450,599,613,599,{arrow:true,sw:1.3});f.text(765,606,'Algorithm 2',16,{anchor:'middle'});
f.notes='This figure follows Algorithm 1 (Graph state generation) in docs/Approach.md, Section 3.2. Inputs: skeleton S, selected pattern p, positive scale N, applicable state variant v, and data seed k_G. Panel (a) selects the prescribed finite recipe rho and records its node/relationship groups, multiplicities, endpoint maps and property facts; unavailable combinations are rejected. Panel (b) constructs the fixed anchor and low-cost groups L, and high-cost groups H with recipe-defined multiplicities n_U(N) and n_F(N). The graph sketch is schematic, not a common mandatory topology. Node groups receive labels, typed properties, absent properties where required, and fixed witness nodes. Relationship groups receive types, endpoint correspondences and multiplicities. Inverse groups, when required, reverse endpoints and use one-to-one common pair keys. Reference groups R, when required, share the designated anchors and preserve witnesses, bounded matching multiplicities and non-decisive-branch matches; their nodes need not be a subset or copy of H. The three nodes drawn in the bounded R region illustrate the implemented short-circuit recipe; H nodes and ellipsis represent a scalable population. k_G selects a pseudorandom identifier offset; each group assigns its own prescribed sequence, with fixed anchor and witness keys retained. Panel (c) packages G as a construction specification including schema, role bindings, parameters and index requirements, and derives I as expected population, uniqueness, property/witness and applicable correspondence facts. These conditions are recorded, not executed here. Index readiness is checked later, separately from I. The output (G,I) is passed to Algorithm 2. No query pair, materialization, database validation, result or timing evidence is represented.';
fs.writeFileSync(path.join(__dirname,'algorithm1_geometry.json'),JSON.stringify([f],null,2)+'\n');console.log('Algorithm 1 specification authored.');

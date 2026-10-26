// Native editable diagram of Algorithm 2 only: population and query-pair construction.
// Existing SVG illustrations are intentionally not changed by this authoring source.
const {Canvas}=require('skia-canvas');
const fs=require('fs'),path=require('path');
const context=new Canvas(8,8).getContext('2d');
const BLACK='#000000',WHITE='#FFFFFF',RED='#B32025';
class Fig {
 constructor(name,h,title){this.name=name;this.width=1000;this.height=h;this.widthMm=180;this.heightMm=.18*h;this.title=title;this.source='docs/Approach.md: Algorithm 2';this.elements=[];this.groupName='canvas';this.rect(0,0,1000,h,{stroke:'none'});}
 group(g){this.groupName=g;}
 add(type,attrs,extra={}){const index=this.elements.length;this.elements.push({type,attrs,index,group:this.groupName,name:`${this.groupName}_${type}_${index}`,...extra});}
 rect(x,y,w,h,o={}){this.add('rect',{x,y,width:w,height:h,rx:o.r||0,fill:o.fill||WHITE,stroke:o.stroke||BLACK,'stroke-width':o.sw||.8,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 circle(x,y,r=16,o={}){this.add('circle',{cx:x,cy:y,r,fill:WHITE,stroke:o.color||BLACK,'stroke-width':o.sw||1.25,...(o.dash?{'stroke-dasharray':o.dash}:{})});}
 line(x1,y1,x2,y2,o={}){this.add('line',{x1,y1,x2,y2,stroke:o.color||BLACK,'stroke-width':o.sw||1.1,...(o.arrow?{'marker-end':'url(#triangle)'}:{})});}
 text(x,y,value,size=16,o={}){
  const font=o.font||'Arial',bold=!!o.bold,italic=!!o.italic,color=o.color||BLACK;
  const pieces=value.replace(/\s+/g,' ').split(/(Q_[12]|V_x|E_[HR]|↦)/).filter(Boolean), runs=[];
  function measure(t,face,sz){context.font=`${italic?'italic ':''}${bold?'bold ':''}${sz}px "${face}"`;return context.measureText(t);}
  for(const piece of pieces){
   if(/^(Q_[12]|V_x|E_[HR])$/.test(piece)){for(const [t,sz,dy]of [[piece[0],size,0],[piece.slice(-1),size*.72,size*.22]]){const m=measure(t,font,sz);runs.push({text:t,font,size:sz,bold,italic,color,dy,advance:m.width,m});}}
   else {const face=piece==='↦'?'Menlo':font,m=measure(piece,face,size);runs.push({text:piece,font:face,size,bold,italic,color,dy:0,advance:m.width,m,fallback:piece==='↦'});}
  }
  const width=runs.reduce((a,r)=>a+r.advance,0);let left=x-(o.anchor==='middle'?width/2:o.anchor==='end'?width:0),loX=Infinity,loY=Infinity,hiX=-Infinity,hiY=-Infinity;
  for(const r of runs){r.x=left;r.y=y+r.dy;left+=r.advance;loX=Math.min(loX,r.x-r.m.actualBoundingBoxLeft);loY=Math.min(loY,r.y-r.m.actualBoundingBoxAscent);hiX=Math.max(hiX,r.x+r.m.actualBoundingBoxRight);hiY=Math.max(hiY,r.y+r.m.actualBoundingBoxDescent);delete r.m;delete r.dy;}
  this.add('text',{x,y,fill:color,'font-family':font,'font-size':size,'font-weight':bold?700:400,'font-style':italic?'italic':'normal','text-anchor':o.anchor||'start'},
    {text:value.replace(/([QVE])_([12xHR])/g,'$1$2'),runs,bbox:{x:loX,y:loY,w:hiX-loX,h:hiY-loY}});
 }
 math(x,y,t,size=19,o={}){this.text(x,y,t,size,{font:'Times New Roman',italic:true,...o});}
 code(x,y,t,size=15,o={}){this.text(x,y,t,size,{font:'Arial',...o});}
 frame(x,w,label,h=250){this.rect(x,8,w,h);this.text(x+w/2,32,label,17,{bold:true,anchor:'middle'});this.line(x,46,x+w,46,{sw:.7});}
 arrow(x1,x2,y,lines=[],under){this.line(x1,y,x2,y,{sw:1.35,arrow:true});lines.forEach((t,i)=>this.text((x1+x2)/2,y-16-(lines.length-i-1)*17,t,14.5,{anchor:'middle'}));if(under)this.math((x1+x2)/2,y+25,under,18,{anchor:'middle'});}
}
const f=new Fig('Algorithm_2_query_pair_construction',644,'Algorithm 2: skeleton population and query-pair construction');
// Stage A follows lines 1–13. Pool alternatives are selected by role(x).
f.group('population');f.rect(8,8,984,262);f.text(26,33,'(a) Skeleton population',18,{bold:true});
f.text(620,33,'Input:',15);f.math(668,33,'S, p, k, d;',18);f.math(795,33,'(G, I)',18);f.text(980,33,'from Algorithm 1',15,{anchor:'end'});f.line(8,46,992,46,{sw:.7});
f.text(105,75,'Initialize',15,{bold:true,anchor:'middle'});
f.rect(60,90,90,48,{sw:1});f.math(86,121,'S',24,{anchor:'middle'});f.rect(110,102,25,25,{stroke:RED,dash:'3 2',sw:.9});f.math(122.5,121,'x',18,{color:RED,anchor:'middle'});
f.line(105,139,105,153,{arrow:true});f.text(123,151,'copy',14.5);
f.rect(60,158,90,48,{sw:1});f.math(85,188,'Q_1',23,{anchor:'middle'});f.rect(110,170,25,25,{stroke:RED,dash:'3 2',sw:.9});f.math(122.5,189,'x',18,{color:RED,anchor:'middle'});
f.math(105,236,'β = {}',18,{anchor:'middle'});
f.line(150,181,183,181);f.line(183,181,183,143);f.line(183,143,213,143,{arrow:true});
f.text(310,64,'Choose by role(x)',15,{anchor:'middle'});
f.line(213,91,213,197);
for(const cy of [91,144,197])f.line(213,cy,225,cy,{arrow:true});
for(const [y,role]of [[70,'L'],[123,'H'],[176,'']]){
 f.rect(225,y,175,42,{sw:.8});f.math(244,y+28,role,21,{anchor:'middle'});
 f.line(400,y+21,415,y+21);}
f.line(415,91,415,197);f.line(415,143,445,143,{arrow:true});
// L: constants or selective/small graph forms. H: forms accessing the H region.
f.rect(266,80,28,24,{sw:.7});f.text(280,98,'0',16,{anchor:'middle'});
f.line(322,93,340,83);f.line(322,93,349,101);for(const [x,y]of [[322,93],[340,83],[349,101]])f.circle(x,y,4,{sw:.8});
f.text(382,99,'…',20,{anchor:'middle'});
f.text(333,150,'Forms over H',15,{anchor:'middle'});
f.text(312,191,'Structural choices',14.5,{anchor:'middle'});f.text(312,210,'labels · types · bounds',13.5,{anchor:'middle'});
f.math(310,239,'V_x',20,{anchor:'middle'});
// Filtering retains only permitted candidates; an empty pool is a failure.
f.rect(445,99,133,99,{sw:.9});f.text(480,121,'Filter',16);f.math(532,121,'V_x',18,{anchor:'middle'});
f.text(511,145,'kind · type',15,{anchor:'middle'});f.text(511,168,'scope · G, I',15,{anchor:'middle'});
f.text(511,190,'nonempty pool',14.5,{anchor:'middle'});
f.line(578,143,621,143,{arrow:true,sw:1.35});
// Seeded choice and bounded equivalent wrapping, then substitution/binding update.
f.rect(621,70,237,173,{sw:.9});
f.text(672,93,'select (k)',15,{anchor:'middle'});f.rect(651,105,42,34,{stroke:RED,sw:1});f.math(672,130,'e',23,{anchor:'middle',color:RED});
f.line(701,122,736,122,{arrow:true});
f.rect(743,91,100,62,{sw:.8});f.rect(754,101,78,42,{sw:.8});f.rect(767,111,52,22,{stroke:RED,sw:.9});f.math(793,129,'e',20,{anchor:'middle',color:RED});
f.text(739,174,'At most d equivalent wrappers',14.5,{anchor:'middle'});
f.math(739,198,'Q_1[x ↦ e];  β[x] ← e',17,{anchor:'middle'});
f.text(739,230,'update variable bindings',14.5,{anchor:'middle'});
// The feedback edge denotes the next placeholder in binding-dependency order.
f.line(739,243,739,255);f.line(739,255,201,255);f.line(201,255,201,143,{arrow:true});
f.rect(424,246,200,19,{stroke:'none'});f.text(524,260,'next x · dependency order',14,{anchor:'middle'});
f.arrow(858,892,143);
f.rect(895,108,80,65,{sw:1});f.math(916,148,'Q_1',25,{anchor:'middle'});f.rect(943,126,23,25,{stroke:RED,sw:1});f.math(954.5,145,'e',18,{color:RED,anchor:'middle'});f.text(935,96,'all x bound',14.5,{anchor:'middle'});
f.line(935,174,935,204,{arrow:true});
f.rect(875,207,108,40,{sw:.9});f.text(929,224,'valid Q₁',14.5,{anchor:'middle'});f.text(929,240,'applicable p',14.5,{anchor:'middle'});
// The admitted base query is copied once, then only one branch is taken.
f.group('copy-and-selector');f.text(26,298,'(b) Query-pair construction',18,{bold:true});f.line(929,248,929,289);f.line(929,289,608,289,{arrow:true});
f.rect(396,273,212,33,{sw:1});f.math(502,296,'Q_2 ← copy(Q_1)',19,{anchor:'middle'});
f.line(502,307,502,314,{arrow:true});
for(const [a,b]of [[[502,315],[557,333]],[[557,333],[502,351]],[[502,351],[447,333]],[[447,333],[502,315]]])f.line(...a,...b,{sw:1});
f.text(502,338,'type(p)',16,{anchor:'middle'});
f.line(447,333,245,333);f.line(245,333,245,360,{arrow:true});f.text(315,326,'SY',16,{bold:true,anchor:'middle'});
f.line(557,333,755,333);f.line(755,333,755,360,{arrow:true});f.text(689,326,'SC',16,{bold:true,anchor:'middle'});
// These are illustrative local instances for the selected family, not composed mutations.
f.group('symmetry');f.rect(20,362,445,160,{sw:.9});f.text(35,385,'Symmetry transformation',17,{bold:true});f.text(450,385,'e.g., P3',14.5,{anchor:'end'});
for(const [xs,labels]of [[[95,185],['u','v']],[[325,415],['v','u']]]){
 f.line(xs[0]+12,448,xs[1]-12,448);f.text((xs[0]+xs[1])/2,428,'GSP_LINK*1..3',13.5,{anchor:'middle'});
 for(let i=0;i<2;i++){const x=xs[i],t=labels[i],col=xs[0]===325?RED:BLACK;f.circle(x,448,12,{color:col});f.math(x,454,t,18,{color:col,anchor:'middle'});f.text(x,483,t==='u'?':GspLow':':GspHigh',14,{anchor:'middle'});if(t==='u')f.text(x,505,'{id:1}',14,{anchor:'middle'});}
}
f.math(48,454,'Q_1',18,{anchor:'middle'});f.math(291,454,'Q_2',18,{anchor:'middle'});f.line(233,448,274,448,{arrow:true,sw:1.3});
// S7 provides a decisive non-null prefix; only the designated H reference changes.
f.group('reference');f.rect(535,362,445,160,{sw:.9});f.text(550,385,'Reference substitution',17,{bold:true});f.text(965,385,'e.g., S7',14.5,{anchor:'end'});
function coalesce(cx,region){
 const y=414,cy=455,xs=[cx-55,cx-5,cx+65];
 for(const x of xs)f.line(cx,y+12,x,cy-12,{sw:1});
 f.rect(cx-50,y-12,100,25,{r:2,sw:1});f.text(cx,y+6,'coalesce',15,{anchor:'middle'});
 f.rect(xs[0]-21,cy-12,42,24,{r:2,sw:1});f.text(xs[0],cy+5,'NULL',13.5,{anchor:'middle'});
 f.circle(xs[1],cy,11);f.text(xs[1],cy+5,'0',15,{anchor:'middle'});
 f.rect(xs[2]-28,cy-12,56,24,{r:2,sw:1});f.text(xs[2],cy+5,'COUNT',13.5,{anchor:'middle'});
 f.line(xs[2],cy+12,xs[2],484);f.circle(xs[2],496,11,{color:region==='R'?RED:BLACK});f.math(xs[2],502,region,17,{anchor:'middle',color:region==='R'?RED:BLACK});
}
coalesce(630,'H');coalesce(850,'R');f.math(565,420,'Q_1',17,{anchor:'middle'});f.math(785,420,'Q_2',17,{anchor:'middle'});
f.line(707,496,903,496,{arrow:true,sw:1.3,color:RED});f.math(805,487,'H ↦ R',16.5,{anchor:'middle',color:RED});
// Both exclusive branches rejoin before the final structural guard and return.
f.group('return');f.line(245,523,245,538);f.line(755,523,755,538);f.line(245,538,755,538);f.line(502,538,502,545,{arrow:true});
f.rect(345,548,314,34,{sw:.9});f.text(502,570,'Q₂ valid; structurally distinct from Q₁',15.5,{anchor:'middle'});
f.text(975,570,'Guard failure: reject',15,{anchor:'end',color:RED});
f.line(502,583,502,592,{arrow:true});
f.rect(8,595,984,41,{sw:1});f.text(27,621,'Output',17,{bold:true});f.math(277,621,'(G, I, Q_1, Q_2, μ)',22,{anchor:'middle'});f.math(790,621,'μ = (p, k, d, β)',21,{anchor:'middle'});
f.notes='This figure visualizes only Algorithm 2 (Skeleton population and query-pair construction) in Approach Section 3.3. Inputs are S, selected pattern p, graph specification and construction conditions (G,I) from Algorithm 1, query seed k, and additional wrapping bound d in {0,1,2}. Initialize Q1 as a copy of S and beta as an empty map. Process placeholders in binding-dependency order: select the role-specific pool (low-cost constants/selective or small regions; forms over H for high-cost roles; structural choices prescribed by S). Filter by syntactic category, type, scope and G/I compatibility; reject an empty pool. Select e using k and apply at most d equivalent wrappers only where permitted; bind x to e, record beta[x], and update variable bindings. After all placeholders are bound, reject an ill-formed Q1 or inapplicable p. Copy Q1 independently into Q2, then take exactly one type(p) branch. The P3 local pattern illustrates the symmetry branch, retaining endpoint constraints and the common reversal-invariant predicate/projection. The S7 local expression is a separate illustrative instance of the reference-substitution branch: its non-null 0 prefix shields the fallback COUNT expression, and only the designated H reference changes to R. These are conditional alternatives, not transformations composed on one concrete query. COUNT-H/R denotes an abbreviated correlated subquery, not complete Cypher syntax. Reject an ill-formed Q2 or a variant structurally identical to Q1; return (G,I,Q1,Q2,mu), mu=(p,k,d,beta). G and I are carried forward unchanged. This figure does not depict database execution, returned query data, timing or performance checking.';
fs.writeFileSync(path.join(__dirname,'algorithm2_geometry.json'),JSON.stringify([f],null,2)+'\n');
console.log('Algorithm 2 specification authored.');

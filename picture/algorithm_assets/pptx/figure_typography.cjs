// Shared publication typography for both algorithm figures. Coordinates are source units.
// At 180 mm/1000 units, 1 source unit = 0.510236 pt.
const FONT={body:'Arial',math:'Times New Roman',symbols:'Menlo'};
const SIZE={heading:18,subheading:16,body:15,detail:14,math:18,display:20,key:24};
const sub=/^(Q_[12]|V_x|E_[HR]|k_G|n_[UF]|T_[fr])$/;
const scalar=/^(S|p|N|v|k|d|x|e|G|I|L|H|R|u|β|ρ|μ)$/;
const tokens=/(e\.g\.|Q_[12]|V_x|E_[HR]|k_G|n_[UF]|T_[fr]|↦|∅|\bcopy\b|\b(?:S|p|N|v|k|d|x|e|G|I|L|H|R|u)\b|[βρμ])/;
function normalizeSize(size,font,bold,value){
 if(font===FONT.math){if(size>=23 && /^(S|Q_[12]|G|I|ρ)$/.test(value))return SIZE.key;return size>=20?SIZE.display:SIZE.math;}
 if(/^[…⋮]$/.test(value))return SIZE.display;
 if(bold)return size>=18?SIZE.heading:SIZE.subheading;
 return size<=14.5?SIZE.detail:SIZE.body;
}
function typeset(ctx,x,y,value,size,o={}){
 value=value.replace(/Q₁/g,'Q_1').replace(/Q₂/g,'Q_2').replace(/\s+/g,' ');
 if(!value)return null;
 const font=o.font||FONT.body,bold=!!o.bold,italic=!!o.italic,color=o.color||'#000000';
 size=o.exactSize||normalizeSize(size,font,bold,value);
 const runs=[];
 function push(t,face,sz,isItalic,isBold,dy=0,fallback=false){
  ctx.font=`${isItalic?'italic ':''}${isBold?'bold ':''}${sz}px "${face}"`;
  const m=ctx.measureText(t);runs.push({text:t,font:face,size:sz,bold:isBold,italic:isItalic,color,dy,advance:m.width,m,fallback});
 }
 for(const piece of value.split(tokens).filter(Boolean)){
  if(sub.test(piece)){
   push(piece[0],FONT.math,size,true,false);
   const suffix=piece.slice(-1);push(suffix,FONT.math,size*.72,!/\d/.test(suffix),false,size*.22);
  }else if(scalar.test(piece))push(piece,FONT.math,size,true,false);
  else if(piece==='↦'||piece==='∅')push(piece,FONT.symbols,size,false,false,0,true);
  else if(piece==='copy' && font===FONT.math)push(piece,FONT.math,size,false,false);
  else push(piece,font,size,italic,bold);
 }
 const width=runs.reduce((a,r)=>a+r.advance,0);let left=x-(o.anchor==='middle'?width/2:o.anchor==='end'?width:0),loX=Infinity,loY=Infinity,hiX=-Infinity,hiY=-Infinity;
 for(const r of runs){r.x=left;r.y=y+r.dy;left+=r.advance;loX=Math.min(loX,r.x-r.m.actualBoundingBoxLeft);loY=Math.min(loY,r.y-r.m.actualBoundingBoxAscent);hiX=Math.max(hiX,r.x+r.m.actualBoundingBoxRight);hiY=Math.max(hiY,r.y+r.m.actualBoundingBoxDescent);delete r.m;delete r.dy;}
 return {attrs:{x,y,fill:color,'font-family':font,'font-size':size,'font-weight':bold?700:400,'font-style':italic?'italic':'normal','text-anchor':o.anchor||'start'},text:value.replace(/([QVEknT])_([12xHRGUFrf])/g,'$1$2'),runs,bbox:{x:loX,y:loY,w:hiX-loX,h:hiY-loY}};
}
function translateGroup(fig,group,dx,dy=0){
 for(const e of fig.elements.filter(e=>e.group===group)){
  const a=e.attrs;if(e.type==='line'){a.x1+=dx;a.x2+=dx;a.y1+=dy;a.y2+=dy;}
  else if(e.type==='circle'){a.cx+=dx;a.cy+=dy;}
  else {a.x+=dx;a.y+=dy;}
  if(e.type==='text'){for(const r of e.runs){r.x+=dx;r.y+=dy;}e.bbox.x+=dx;e.bbox.y+=dy;}
 }
}
module.exports={FONT,SIZE,typeset,translateGroup};

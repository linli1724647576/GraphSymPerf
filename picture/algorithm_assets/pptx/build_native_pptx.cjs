// Reproduce the approved SVGs as PowerPoint-native shapes and text.
// Usage: NODE_PATH=<bundled node_modules> node build_native_pptx.cjs [output directory]
const pptxgen=require('pptxgenjs');
const fs=require('fs'),path=require('path');
const {calcTextBox}=require('./pptxgenjs_helpers/text');
const {warnIfSlideHasOverlaps,warnIfSlideElementsOutOfBounds}=require('./pptxgenjs_helpers/layout');
const figures=JSON.parse(fs.readFileSync(path.join(__dirname,'svg_geometry.json')));
const output=process.argv[2]||'/tmp/graphsymperf-native-pptx';fs.mkdirSync(output,{recursive:true});
const correctionPath=path.join(__dirname,'text_calibration.json');
const calibration=fs.existsSync(correctionPath)?JSON.parse(fs.readFileSync(correctionPath)):{};
const hex=s=>s==='none'?null:(s.startsWith('#')?s.slice(1):s.match(/\d+/g).slice(0,3).map(v=>Number(v).toString(16).padStart(2,'0')).join('')).toUpperCase();
const asciifont={Arial:0.905273,'Times New Roman':0.891113,'Courier New':0.832520,'Menlo':1.026855};
function lineStyle(a,pt){return {color:hex(a.stroke)||'FFFFFF',width:Number(a['stroke-width']||1)*pt,transparency:a.stroke==='none'?100:0,...(a['stroke-dasharray']?{dashType:'dash'}:{})};}
(async()=>{
 const manifest=[];
 for(const fig of figures){
  const pptx=new pptxgen();const scale=fig.widthMm/25.4/fig.width,pt=scale*72;
  pptx.defineLayout({name:'SOURCE',width:fig.width*scale,height:fig.height*scale});pptx.layout='SOURCE';
  pptx.author='GraphSymPerf';pptx.subject='Native editable reconstruction of approved SVG';pptx.title=fig.title;pptx.company='GraphSymPerf';pptx.lang='en-US';
  pptx.theme={headFontFace:'Arial',bodyFontFace:'Arial',lang:'en-US'};
  const slide=pptx.addSlide();slide.background={color:'FFFFFF'};
  let textCount=0,shapeCount=0;let textObjects=[];let objectMap=[];
  function geom(type,opts,name){slide.addShape(type,{...opts,objectName:name});shapeCount++;}
  function triangle(points,name,color){
    const x=Math.min(...points.map(p=>p[0])),y=Math.min(...points.map(p=>p[1]));
    const w=Math.max(...points.map(p=>p[0]))-x,h=Math.max(...points.map(p=>p[1]))-y;
    geom(pptx.ShapeType.custGeom,{x:x*scale,y:y*scale,w:w*scale,h:h*scale,
      points:[...points.map(([px,py])=>({x:(px-x)*scale,y:(py-y)*scale})),{close:true}],
      fill:{color},line:{color,transparency:100}},name);
  }
  for(const el of fig.elements){
   const a=el.attrs,n=el.name;objectMap.push({source:el.index,name:n,type:el.type,group:el.group});
   if(el.type==='rect'){
    geom(Number(a.rx)>0?pptx.ShapeType.roundRect:pptx.ShapeType.rect,
      {x:+a.x*scale,y:+a.y*scale,w:+a.width*scale,h:+a.height*scale,
       rectRadius:+(a.rx||0)*scale,line:lineStyle(a,pt),fill:{color:hex(a.fill)||'FFFFFF',transparency:a.fill==='none'?100:0}},n);
   }else if(el.type==='circle'){
    geom(pptx.ShapeType.ellipse,{x:(+a.cx-a.r)*scale,y:(+a.cy-a.r)*scale,w:2*a.r*scale,h:2*a.r*scale,
      line:lineStyle(a,pt),fill:{color:hex(a.fill)||'FFFFFF'}},n);
   }else if(el.type==='line'){
    const x1=+a.x1,y1=+a.y1,x2=+a.x2,y2=+a.y2;
    geom(pptx.ShapeType.line,{x:Math.min(x1,x2)*scale,y:Math.min(y1,y2)*scale,w:Math.abs(x2-x1)*scale,h:Math.abs(y2-y1)*scale,
      flipH:x2<x1,flipV:y2<y1,line:lineStyle(a,pt)},n);
    if(a['marker-end']){
      const theta=Math.atan2(y2-y1,x2-x1),c=Math.cos(theta),s=Math.sin(theta);
      const pts=[[-6.5,-3.5],[.5,0],[-6.5,3.5]].map(([x,y])=>[x2+x*c-y*s,y2+x*s+y*c]);
      triangle(pts,n+'_arrowhead',hex(a.stroke));
    }
   }else if(el.type==='text'){
    // Native text fragments preserve the exact SVG baselines, including subscripts.
    for(const [i,r] of el.runs.entries()){
      const font=r.fallback?'Menlo':r.font;
      const key=font+'|'+(r.bold?'bold':'normal')+'|'+(r.italic?'italic':'normal');
      const fontPt=r.size*pt;
      const ascent=asciifont[font]||.9;
      const adjust=(calibration[key]||0)*r.size;
      const nominalTop=(r.y-r.size*ascent-adjust)*scale;
      const width=(Math.max(r.advance,1)+Math.max(5,r.size*.45))*scale;
      const layout=calcTextBox(fontPt,{text:r.text,fontFace:font,w:width,margin:0,paraSpaceAfter:0,padding:0,leading:1.15});
      const opts={x:r.x*scale,y:nominalTop,w:width,h:Math.max(layout.h,fontPt/72*1.15),
       fontFace:font,fontSize:fontPt,bold:r.bold,italic:r.italic,color:hex(r.color),margin:0,
       breakLine:false,paraSpaceBeforePt:0,paraSpaceAfterPt:0,lineSpacingMultiple:1.0,wrap:false,
       valign:'top',align:'left',isTextBox:true,objectName:n+'_run'+i};
      slide.addText(r.text,opts);textCount++;
      const obj=slide._slideObjects[slide._slideObjects.length-1];
      textObjects.push(obj);
      objectMap.push({name:opts.objectName,text:r.text,key,fontPt,sourceBaseline:r.y,sourceX:r.x,sourceSize:r.size,sourceAdvance:r.advance});
    }
   }else throw Error('Unsupported source geometry: '+el.type);
  }
  // Overlap between text and its containing node, stacked circles, panel frames,
  // and marker triangles is intentional and exactly preserves the approved SVG.
  // Native em-boxes for a baseline and subscript can overlap intentionally.
  // Check the measured visible label bounds; the exported PPTX is also render-audited.
  const textAudit={_slideObjects:fig.elements.filter(e=>e.type==='text').map(e=>({type:'text',options:{x:e.bbox.x*scale,y:e.bbox.y*scale,w:e.bbox.w*scale,h:e.bbox.h*scale}}))};
  warnIfSlideHasOverlaps(textAudit,pptx,{muteContainment:true});
  warnIfSlideElementsOutOfBounds(slide,pptx);
  const name=fig.name+'.pptx';await pptx.writeFile({fileName:path.join(output,name)});
  const item={file:name,widthMm:fig.widthMm,heightMm:fig.heightMm,source:fig.name+'.svg',shapeCount,textCount,objectMap};
  manifest.push(item);console.log(name+': '+shapeCount+' native shapes, '+textCount+' native text fragments');
 }
 fs.writeFileSync(path.join(__dirname,'native_object_manifest.json'),JSON.stringify(manifest,null,2)+'\n');
})();

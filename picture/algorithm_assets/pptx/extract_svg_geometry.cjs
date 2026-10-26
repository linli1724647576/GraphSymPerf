// Capture editable SVG primitives and actual browser text baselines, including tspans.
const fs=require('fs'), path=require('path');
const {chromium}=require('playwright');
const base=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1200,height:800}});
 let figures=[];
 for(const name of ['Algorithm_1_graph_state_generation','Algorithm_2_query_pair_construction']){
  await page.goto('file://'+path.join(base,name+'.svg'));
  const fig=await page.evaluate(()=>{
   const svg=document.querySelector('svg'),v=svg.viewBox.baseVal;
   const elements=[];
   const list=[...svg.querySelectorAll('rect,circle,line,path,text')].filter(e=>!e.closest('defs'));
   for(const [index,e] of list.entries()){
    const attrs=Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]));
    const group=e.closest('g')?.id||'ungrouped';
    const el={type:e.tagName,attrs,index,group,name:group+'_'+e.tagName+'_'+index};
    if(e.tagName==='text'){
     let idx=0;el.runs=[];
     const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
     for(let t=walker.nextNode();t;t=walker.nextNode()){
      if(!t.data.length)continue;
      const style=getComputedStyle(t.parentElement);
      // Split the unsupported math arrow so its host fallback is explicit in PPT.
      const parts=t.data.replace(/\s+/g,' ').split(/(↦)/).filter(Boolean);
      for(const str of parts){
       const start=e.getStartPositionOfChar(idx),end=e.getEndPositionOfChar(idx+str.length-1);
       el.runs.push({text:str,x:start.x,y:start.y,advance:end.x-start.x,
        font:style.fontFamily.split(',')[0].replaceAll('"',''),size:parseFloat(style.fontSize),
        bold:parseInt(style.fontWeight)>=600,italic:style.fontStyle==='italic',color:style.fill,
        fallback:str==='↦'});
       idx+=str.length;
      }
     }
     el.text=e.textContent;const b=e.getBBox();el.bbox={x:b.x,y:b.y,w:b.width,h:b.height};
    }
    elements.push(el);
   }
   return {width:v.width,height:v.height,widthMm:parseFloat(svg.getAttribute('width')),heightMm:parseFloat(svg.getAttribute('height')),title:svg.querySelector('title').textContent,elements};
  });
  fig.name=name;figures.push(fig);
 }
 fs.writeFileSync(path.join(__dirname,'svg_geometry.json'),JSON.stringify(figures,null,2)+'\n');
 await browser.close(); console.log('SVG geometry and text baselines extracted.');
})();

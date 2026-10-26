// Usage: NODE_PATH=<directory containing playwright> node render_algorithm_figures.cjs
// Uses an isolated headless browser; it does not access the user's browser profile.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const out = path.resolve(__dirname, '..');
(async () => {
  const options = {headless:true};
  const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if (fs.existsSync(chrome)) options.executablePath = chrome;
  const browser = await chromium.launch(options);
  const page = await browser.newPage({viewport:{width:1200,height:1000},deviceScaleFactor:2});
  const reports=[];
  for (const name of ['Algorithm_1_graph_state_generation','Algorithm_2_query_pair_construction']) {
    await page.goto('file://' + path.join(out, name+'.svg'));
    await page.evaluate(() => { const s=document.querySelector('svg'); s.style.width=s.viewBox.baseVal.width+'px';s.style.height=s.viewBox.baseVal.height+'px';});
    await page.locator('svg').screenshot({path:path.join(__dirname,name+'.png')});
    const report=await page.evaluate(() => {
      const svg=document.querySelector('svg'), v=svg.viewBox.baseVal;
      const text=[...document.querySelectorAll('text')].map(e=>{const b=e.getBBox();return {text:e.textContent, x:b.x,y:b.y,width:b.width,height:b.height};});
      return {text, overflow:text.filter(b=>b.x<0||b.y<0||b.x+b.width>v.width||b.y+b.height>v.height),
        textObjects:text.length, rasterImages:document.querySelectorAll('image').length,
        foreignObjects:document.querySelectorAll('foreignObject').length};
    });
    reports.push({file:name+'.svg',...report});
    console.log(name+': '+report.textObjects+' editable text objects; '+report.overflow.length+' text overflow(s)');
  }
  fs.writeFileSync(path.join(__dirname,'render_validation.json'),JSON.stringify(reports,null,2)+'\n');
  await browser.close();
})();

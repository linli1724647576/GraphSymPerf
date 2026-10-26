/** Render the Chinese method manuscript as self-contained HTML and a vector PDF.
 * Requires marked + playwright; GSYMPERF_NODE_MODULES can point at a bundled runtime.
 * GSYMPERF_CHROME optionally selects an installed Chrome executable.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const directory = path.dirname(fileURLToPath(import.meta.url));
const stem = 'GraphSymPerf-六阶段方法汇报稿';
const packageRoot = process.env.GSYMPERF_NODE_MODULES;
const {marked} = await import(packageRoot
  ? pathToFileURL(path.join(packageRoot, 'marked/lib/marked.esm.js')).href : 'marked');
const {chromium} = await import(packageRoot
  ? pathToFileURL(path.join(packageRoot, 'playwright/index.mjs')).href : 'playwright');

const markdown = await fs.readFile(path.join(directory, `${stem}.md`), 'utf8');
let body = marked.parse(markdown, {gfm: true});
let figureIndex = 0;
for (const match of [...body.matchAll(/<p><img src="([^"]+\.svg)" alt="([^"]*)"><\/p>/g)]) {
  const filename = path.resolve(directory, match[1]);
  if (!filename.startsWith(directory + path.sep)) throw new Error('Figure must be inside docs');
  let svg = await fs.readFile(filename, 'utf8');
  const prefix = `figure${++figureIndex}_`;
  svg = svg.replace(/id="([^"]+)"/g, (_, name) => `id="${prefix}${name}"`)
    .replace(/url\(#([^)]+)\)/g, (_, name) => `url(#${prefix}${name})`)
    .replace(/aria-labelledby="([^"]+)"/g,
      (_, names) => `aria-labelledby="${names.split(' ').map(name => prefix + name).join(' ')}"`);
  body = body.replace(match[0], `<div class="diagram">${svg}</div>`);
}
body = body.replace(/<div class="diagram">([\s\S]*?<\/svg>)\s*<\/div>\s*<p><strong>(图[12]｜[\s\S]*?)<\/p>/g,
  '<figure class="diagram">$1<figcaption><strong>$2</figcaption></figure>');
body = body.replace(/<h2>([1-6]) M([1-6])：([^<]+)<\/h2>/g,
  '<h2 id="M$2">$1 M$2：$3</h2>');
const contents = [...markdown.matchAll(/^## ([1-6]) M([1-6])：(.+)$/gm)]
  .map(item => `<a href="#M${item[2]}"><span>M${item[2]}</span>${item[3]}</a>`).join('');
const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${stem}</title><style>
:root{--ink:#15324f;--blue:#2b6cb0;--green:#2d7a64;--line:#d7e1eb}
*{box-sizing:border-box}body{margin:0;background:#edf1f6;color:#182c40;font-family:'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.85}
article{max-width:980px;margin:36px auto;padding:58px 66px;background:#fff;box-shadow:0 8px 40px #15324f0d}
h1,h2,h3{color:var(--ink);line-height:1.45}h1{font-size:30px;margin:0 0 34px}h2{font-size:24px;margin:42px 0 19px;padding-bottom:9px;border-bottom:2px solid var(--line)}h3{font-size:19px;margin:27px 0 13px}
p{margin:12px 0}a{color:var(--blue);text-decoration:none;overflow-wrap:anywhere}strong{font-weight:650}
blockquote{margin:17px 0;padding:10px 18px;border-left:3px solid var(--green);background:#f4f8f6;line-height:1.7;overflow-wrap:anywhere}blockquote p{margin:2px 0}
code{font-family:'SFMono-Regular',Menlo,Consolas,'PingFang SC',monospace;font-size:.9em;background:#f3f6f9;padding:1px 4px;border-radius:3px}
pre{margin:17px 0;padding:15px 18px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;background:#f4f7fa;border:1px solid #e0e7ef;border-radius:5px}pre code{padding:0;background:none;font-size:13px}p:has(+ pre){break-after:avoid}
table{width:100%;border-collapse:collapse;margin:18px 0;font-size:14px;line-height:1.7}th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}th{background:#edf3f8;color:var(--ink);font-weight:650}tr:nth-child(even) td{background:#fafbfd}
figure.diagram{margin:24px 0 28px}div.diagram{margin:24px 0}.diagram svg{display:block;width:100%;height:auto}figcaption{font-size:13px;line-height:1.7;color:#465d73;margin-top:12px}figcaption strong{font-weight:400}figcaption strong:first-child{font-weight:600}
nav{position:fixed;left:max(15px,calc(50vw - 710px));top:48px;width:186px;font-size:12px;line-height:1.7}nav a{display:block;color:#52677c;margin:0 0 12px}nav span{display:inline-block;color:var(--blue);font-weight:700;margin-right:7px}
@media(max-width:1400px){nav{display:none}}@media(max-width:700px){article{margin:0;padding:25px 22px}h1{font-size:25px}h2{font-size:22px}table{font-size:12px}}
@page{size:A4;margin:18mm 17mm 18mm}
@media print{body{background:white;font-size:10.3pt;line-height:1.72;color:#172a3b}article{max-width:none;margin:0;padding:0;box-shadow:none}nav{display:none}h1{font-size:18pt;margin-bottom:18pt}h2{font-size:14.2pt;margin:24pt 0 11pt;padding-bottom:5pt;break-after:avoid}h3{font-size:11.8pt;margin:17pt 0 7pt;break-after:avoid}p{margin:7pt 0;orphans:3;widows:3}pre{font-size:8.3pt;line-height:1.6;padding:10pt 11pt;margin:10pt 0;break-inside:avoid}pre code{font-size:8.3pt}table{font-size:8.4pt;line-height:1.65;margin:10pt 0}th,td{padding:6pt 7pt}thead{display:table-header-group}tr{break-inside:avoid}figure.diagram{margin:13pt 0 16pt;break-inside:avoid}figcaption{font-size:8.5pt;line-height:1.6;margin-top:7pt}blockquote{padding:7pt 11pt;margin:10pt 0;break-inside:avoid}a{color:#285c83}ul,ol{padding-left:19pt}li{margin:5pt 0}}
</style></head><body><nav aria-label="六阶段目录">${contents}</nav><article>${body}</article></body></html>`;
const htmlPath = path.join(directory, `${stem}.html`);
await fs.writeFile(htmlPath, html);
const chrome = process.env.GSYMPERF_CHROME
  || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
const browser = await chromium.launch({headless:true, ...(chrome ? {executablePath:chrome} : {})});
try {
  const page = await browser.newPage({viewport:{width:1280,height:1000},deviceScaleFactor:1});
  await page.goto(pathToFileURL(htmlPath).href, {waitUntil:'load'});
  await page.evaluate(() => document.fonts.ready);
  const audit = await page.evaluate(() => ({
    figures:document.querySelectorAll('figure svg').length,
    unresolvedImages:[...document.images].filter(image=>!image.complete||image.naturalWidth===0).length,
    horizontalOverflow:document.documentElement.scrollWidth>window.innerWidth,
  }));
  if(audit.figures!==2||audit.unresolvedImages||audit.horizontalOverflow) throw new Error(JSON.stringify(audit));
  await page.pdf({path:path.join(directory,`${stem}.pdf`),printBackground:true,preferCSSPageSize:true,
    displayHeaderFooter:true,headerTemplate:'<span></span>',
    footerTemplate:'<div style="font-size:8px;width:100%;text-align:center;color:#64748b"><span class="pageNumber"></span> / <span class="totalPages"></span></div>'});
  console.log(JSON.stringify({html:htmlPath,pdf:path.join(directory,`${stem}.pdf`),...audit}));
}finally{await browser.close()}

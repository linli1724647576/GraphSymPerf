"""Inspect native PPTX objects and compare exported text baselines with the SVG."""
from pathlib import Path
from collections import Counter
from PIL import Image
from lxml import etree as E
import fitz,json,statistics,zipfile,sys
HERE=Path(__file__).resolve().parent
ROOT=Path(sys.argv[1]); rendered=ROOT/'rendered'
report=[]
for item in json.loads((HERE/'native_object_manifest.json').read_text()):
 pdf=rendered/item['file'].replace('.pptx','.pdf')
 doc=fitz.open(pdf); page=doc[0]
 ref=Image.open(HERE.parent/item['file'].replace('.pptx','.png')).convert('RGB')
 pix=page.get_pixmap(matrix=fitz.Matrix(ref.width/page.rect.width,ref.width/page.rect.width),alpha=False)
 img=Image.frombytes('RGB',[pix.width,pix.height],pix.samples).crop((0,0,ref.width,ref.height))
 img.save(pdf.with_suffix('.png'))
 Image.blend(ref,img,.5).save(rendered/(pdf.stem+'-overlay.png'))
 spans=[s for b in page.get_text('dict')['blocks'] if 'lines' in b for l in b['lines'] for s in l['spans']]
 scale=item['widthMm']/25.4*72/1000;errors=[];missing=[]
 for t in [o for o in item['objectMap'] if 'text' in o]:
  found=[s for s in spans if s['text'].strip()==t['text'].strip()]
  if not found:missing.append(t['text']);continue
  z=min(found,key=lambda z:abs(z['origin'][0]/scale-t['sourceX'])+abs(z['origin'][1]/scale-t['sourceBaseline']))
  errors.append(abs(z['origin'][1]/scale-t['sourceBaseline']))
 with zipfile.ZipFile(ROOT/item['file'])as archive:
  xml=E.fromstring(archive.read('ppt/slides/slide1.xml'))
  tags=Counter(E.QName(e).localname for e in xml.iter())
  media=[n for n in archive.namelist() if n.startswith('ppt/media/') and not n.endswith('/')]
  pres=E.fromstring(archive.read('ppt/presentation.xml'))
  size=pres.find('{http://schemas.openxmlformats.org/presentationml/2006/main}sldSz')
 assert not missing,missing
 assert not media and tags['pic']==0
 assert abs(int(size.get('cx'))/36000-item['widthMm'])<.0001
 assert abs(int(size.get('cy'))/36000-item['heightMm'])<.0001
 row={'file':item['file'],'missing_text':missing,'embedded_images':len(media),
      'native_shapes_including_text':tags['sp'],'text_fragments':tags['txBody'],
      'max_baseline_error_svg_units':max(errors),'median_baseline_error_svg_units':statistics.median(errors),
      'width_mm':int(size.get('cx'))/36000,'height_mm':int(size.get('cy'))/36000,
      'renderer':'LibreOffice PDF export'}
 report.append(row);print(json.dumps(row))
(HERE/'reconstruction_validation.json').write_text(json.dumps(report,indent=2)+'\n')

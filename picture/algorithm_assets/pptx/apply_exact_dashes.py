"""Preserve SVG dash lengths as native OOXML custom dash arrays."""
from pathlib import Path
import json,sys,zipfile
from lxml import etree as E
ns={'p':'http://schemas.openxmlformats.org/presentationml/2006/main','a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
root=Path(__file__).resolve().parent
geometry=Path(sys.argv[2]) if len(sys.argv)>2 else root/'svg_geometry.json'
for fig in json.loads(geometry.read_text()):
 p=Path(sys.argv[1])/f"{fig['name']}.pptx"
 with zipfile.ZipFile(p) as z: files={n:z.read(n) for n in z.namelist()}
 xml=E.fromstring(files['ppt/slides/slide1.xml'])
 dash={e['name']:e['attrs'] for e in fig['elements'] if e['attrs'].get('stroke-dasharray')}
 for shape in xml.findall('.//p:sp',ns):
  name=shape.find('p:nvSpPr/p:cNvPr',ns).get('name')
  if name not in dash:continue
  attrs=dash[name];stroke=float(attrs['stroke-width']);length,gap=map(float,attrs['stroke-dasharray'].split())
  line=shape.find('p:spPr/a:ln',ns)
  for node in list(line):
   if E.QName(node).localname in ['prstDash','custDash']:line.remove(node)
  custom=E.Element('{'+ns['a']+'}custDash')
  E.SubElement(custom,'{'+ns['a']+'}ds',d=str(round(length/stroke*100000)),sp=str(round(gap/stroke*100000)))
  line.append(custom)
 files['ppt/slides/slide1.xml']=E.tostring(xml,xml_declaration=True,encoding='UTF-8',standalone=True)
 with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
  for name,data in files.items():z.writestr(name,data)
 print(p.name, 'custom dash patterns:',len(dash))

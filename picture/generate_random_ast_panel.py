"""Draw an editable M1 panel matching the narrow region in the supplied Overview.

Only the panel is native vector art. The optional Overview mock-up embeds the
unchanged user screenshot as a reference and places the vector panel over its
marked region.
"""

import argparse
import base64
from html import escape
from pathlib import Path
import struct
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
INK = '#171717'
GRAY = '#8A8A8A'
LINE = '#AAAAAA'
FONT = 'Arial,Helvetica,sans-serif'


def draw_panel():
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="330" height="720" '
        'viewBox="0 0 330 720" role="img" aria-labelledby="panel-title panel-desc">',
        '<title id="panel-title">Random typed AST generation: clauses, expansion and an abstract query</title>',
        '<desc id="panel-desc">One example samples MATCH then RETURN, selects COUNT from integer '
        'productions, and forms a Query AST with an unbound label alpha and exactly one typed '
        'result-table slot for later Pattern embedding. Gray dashed productions are alternatives.</desc>',
        '<defs><marker id="m1-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto">'
        f'<path d="M0 0L10 5L0 10Z" fill="{INK}"/></marker>'
        '<marker id="m1-gray-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto">'
        f'<path d="M0 0L10 5L0 10Z" fill="{LINE}"/></marker></defs>',
        '<rect width="330" height="720" fill="white"/>',
        f'<rect x="1.5" y="1.5" width="327" height="717" fill="none" '
        f'stroke="{INK}" stroke-width="1.8" stroke-dasharray="7 6"/>',
    ]

    def text(x, y, value, size=22, color=INK, bold=False, anchor='middle'):
        parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                     f'fill="{color}" font-weight="{700 if bold else 400}" '
                     f'text-anchor="{anchor}">{escape(value)}</text>')

    def path(data, *, chosen=True, arrow=False, width=2):
        parts.append(f'<path d="{data}" fill="none" stroke="{INK if chosen else LINE}" '
                     f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'
                     + ('' if chosen else ' stroke-dasharray="4 4"')
                     + (f' marker-end="url(#{"m1-arrow" if chosen else "m1-gray-arrow"})"' if arrow else '')
                     + '/>')

    def box(x, y, width, height, label, *, chosen=True, size=22, bold=False):
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
                     f'rx="5" fill="white" stroke="{INK if chosen else LINE}" stroke-width="1.8"'
                     + ('' if chosen else ' stroke-dasharray="5 4"') + '/>')
        text(x + width / 2, y + height / 2 + size * .34, label, size,
             INK if chosen else GRAY, bold)

    # Selected clause primitives are solid, the unselected WITH is dashed.
    text(165, 34, 'Sample clauses', 24, bold=True)
    box(13, 59, 91, 37, 'MATCH', size=20, bold=True)
    box(121, 59, 75, 37, 'WITH', chosen=False, size=20)
    box(212, 59, 105, 37, 'RETURN', size=20, bold=True)
    path('M58 98V112H165V126', arrow=True)
    path('M264 98V112H165')
    box(47, 134, 236, 43, 'MATCH → RETURN', size=22, bold=True)
    path('M165 183V202', arrow=True, width=2.5)

    # Expected integer type limits the operator alternatives.
    text(165, 233, 'Typed expansion', 24, bold=True)
    text(165, 260, 'Type · scope · budget', 18, color='#5B5B5B')
    box(94, 281, 142, 40, 'Expr : int', size=22)
    path('M165 323V338H55V350', chosen=False, arrow=True)
    path('M165 338H277V350', chosen=False, arrow=True)
    path('M165 323V350', arrow=True, width=3)
    box(13, 355, 84, 39, 'CASE', chosen=False, size=21)
    box(112, 355, 106, 39, 'COUNT', size=23, bold=True)
    box(233, 355, 84, 39, 'Literal', chosen=False, size=20)
    path('M165 401V426', arrow=True, width=2.5)

    # One complete abstract structure: operators fixed, graph terminals unbound.
    text(165, 462, 'Abstract AST A₀', 24, bold=True)
    box(111, 483, 108, 40, 'Query', size=23, bold=True)
    path('M165 526V537H80V544')
    path('M165 537H251V544')
    box(25, 549, 110, 39, 'MATCH', size=22, bold=True)
    box(195, 549, 112, 39, 'RETURN', size=22, bold=True)
    path('M80 591V603', arrow=True)
    path('M251 591V603', arrow=True)
    box(25, 608, 110, 39, '(n : α)', size=23)
    box(195, 608, 112, 39, 'COUNT', size=23, bold=True)
    path('M251 650V669', arrow=True)
    parts.append(f'<rect x="236" y="675" width="30" height="30" fill="white" '
                 f'stroke="{INK}" stroke-width="2.8"/>')
    text(222, 697, 'table slot', 19, anchor='end')
    parts.append('</svg>')
    result = '\n'.join(parts) + '\n'
    ElementTree.fromstring(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--overview', type=Path, help='User screenshot, 1950 × 858 PNG, for the placement preview')
    options = parser.parse_args()
    panel = draw_panel()
    panel_path = ROOT / 'Random_AST_Generation_panel.svg'
    panel_path.write_text(panel, encoding='utf-8')
    print(panel_path)
    if options.overview:
        original = options.overview.read_bytes()
        if original[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', original[16:24]) != (1950, 858):
            raise ValueError('The placement preview expects the supplied 1950 × 858 PNG')
        embedded = base64.b64encode(original).decode('ascii')
        panel_inner = panel[panel.index('>') + 1:panel.rindex('</svg>')]
        preview = ('<svg xmlns="http://www.w3.org/2000/svg" width="1950" height="858" viewBox="0 0 1950 858">\n'
                   '<title>Overview placement preview: Random AST Generation panel</title>\n'
                   f'<image width="1950" height="858" href="data:image/png;base64,{embedded}"/>\n'
                   '<rect x="7" y="109" width="336" height="730" fill="white"/>\n'
                   '<svg x="10" y="113" width="330" height="720" viewBox="0 0 330 720">\n'
                   f'{panel_inner}</svg>\n</svg>\n')
        ElementTree.fromstring(preview)
        output = ROOT / 'Overview_AST_panel_preview.svg'
        output.write_text(preview, encoding='utf-8')
        print(output)


if __name__ == '__main__':
    main()

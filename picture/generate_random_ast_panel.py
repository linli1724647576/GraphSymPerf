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
FONT = 'Arial,Helvetica,sans-serif'


def draw_panel():
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="330" height="720" '
        'viewBox="0 0 330 720" role="img" aria-labelledby="panel-title panel-desc">',
        '<title id="panel-title">Random AST generation: a random choice and one large query AST</title>',
        '<desc id="panel-desc">A dice symbol represents random generation. A large query AST '
        'contains MATCH and RETURN, with COUNT and unbound terminal leaves. No Pattern or '
        'concrete graph is introduced.</desc>',
        '<defs><marker id="m1-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="17" markerHeight="17" markerUnits="userSpaceOnUse" orient="auto">'
        f'<path d="M0 0L10 5L0 10Z" fill="{INK}"/></marker></defs>',
        '<rect width="330" height="720" fill="white"/>',
        f'<rect x="1.5" y="1.5" width="327" height="717" fill="none" '
        f'stroke="{INK}" stroke-width="1.8" stroke-dasharray="7 6"/>',
    ]

    def text(x, y, value, size=28):
        parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                     f'fill="{INK}" font-weight="700" text-anchor="middle">{escape(value)}</text>')

    def path(data, *, arrow=False, width=3):
        parts.append(f'<path d="{data}" fill="none" stroke="{INK}" stroke-width="{width}" '
                     'stroke-linejoin="round" stroke-linecap="round"'
                     + (' marker-end="url(#m1-arrow)"' if arrow else '') + '/>')

    def box(x, y, width, height, label, size=28):
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
                     f'rx="7" fill="white" stroke="{INK}" stroke-width="2.8"/>')
        text(x + width / 2, y + height / 2 + size * .34, label, size)

    # Randomness is a single visual symbol, rather than another text workflow.
    parts.append(f'<rect x="127" y="24" width="76" height="76" rx="11" '
                 f'fill="white" stroke="{INK}" stroke-width="3.5"/>')
    for x, y in ((143, 40), (187, 40), (165, 62), (143, 84), (187, 84)):
        parts.append(f'<circle cx="{x}" cy="{y}" r="5.7" fill="{INK}"/>')
    path('M165 119V179', arrow=True, width=4.5)

    # One large AST occupies the remaining panel. Only operator names remain.
    box(98, 196, 134, 64, 'Query', 30)
    path('M165 263V306H79V347')
    path('M165 306H251V347')
    box(12, 350, 134, 66, 'MATCH')
    box(184, 350, 134, 66, 'RETURN')
    path('M79 419V502')
    path('M251 419V500')
    parts.append(f'<circle cx="79" cy="535" r="30" fill="white" '
                 f'stroke="{INK}" stroke-width="2.8"/>')
    text(79, 546, 'α', 32)
    box(194, 503, 114, 64, 'COUNT')
    path('M251 570V641')
    parts.append(f'<rect x="223" y="645" width="56" height="56" fill="white" '
                 f'stroke="{INK}" stroke-width="3.2"/>')
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

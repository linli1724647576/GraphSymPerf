"""Add a few unselected productions to the user's minimal AST illustration."""

from html import escape
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
INK = '#111111'
GRAY = '#A2A2A2'
FONT = 'Arial,Helvetica,sans-serif'


def main():
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1420" '
             'viewBox="0 0 600 1420" role="img" aria-labelledby="title desc">',
             '<title id="title">Random AST generation with selected and alternative productions</title>',
             '<desc id="desc">The black tree is the chosen AST. A dashed gray edge pattern '
             'is an alternative to the selected node matching; CASE and coalesce are '
             'alternative integer expressions to the selected COUNT. Gray branches are '
             'choices, not simultaneous children of the generated AST.</desc>',
             '<rect width="600" height="1420" fill="white"/>']

    def line(data, *, gray=False, width=3.2):
        parts.append(f'<path d="{data}" fill="none" stroke="{GRAY if gray else INK}" '
                     f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"'
                     + (' stroke-dasharray="8 7"' if gray else '') + '/>')

    def text(x, y, value, size=44, *, gray=False, weight=700, anchor='middle'):
        parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                     f'font-weight="{weight}" fill="{GRAY if gray else INK}" '
                     f'text-anchor="{anchor}">{escape(value)}</text>')

    def box(x, y, width, height, label, size=44, *, gray=False):
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="15" '
                     f'fill="white" stroke="{GRAY if gray else INK}" stroke-width="{2.4 if gray else 3}"'
                     + (' stroke-dasharray="8 7"' if gray else '') + '/>')
        text(x + width / 2, y + height / 2 + .34 * size, label, size, gray=gray)

    # Keep the user's title, shuffle glyph, large nodes and broad spacing.
    parts.append(f'<circle cx="65" cy="107" r="45" fill="white" stroke="{INK}" stroke-width="4"/>')
    text(65, 132, '1', 70, weight=400)
    text(122, 92, 'Random AST', 68, anchor='start')
    text(122, 174, 'Generation', 66, anchor='start')
    parts.append(f'<rect x="22" y="192" width="542" height="1200" fill="none" '
                 f'stroke="{INK}" stroke-width="3.5" stroke-dasharray="13 11"/>')
    line('M220 278H246L273 305 M311 342L345 376H378', width=21)
    line('M220 376H246L345 278H378', width=21)
    line('M356 254L380 278L356 302', width=21)
    line('M356 352L380 376L356 400', width=21)
    box(199, 455, 192, 95, 'Query', 46)

    line('M295 553V612H154V671')
    line('M295 612H430V671')
    box(48, 675, 212, 95, 'MATCH', 45)
    box(324, 675, 212, 95, 'RETURN', 43)

    # MATCH chooses one node pattern; a single-edge pattern is left unselected.
    # Longer chains are deliberately not drawn as a single MATCH atom.
    line('M154 773V979', width=4)
    line('M154 809H228V848', gray=True, width=2.8)
    line('M215 875H241', gray=True, width=2.8)
    for x in (201, 255):
        parts.append(f'<circle cx="{x}" cy="875" r="14" fill="white" '
                     f'stroke="{GRAY}" stroke-width="2.8" stroke-dasharray="5 4"/>')
    box(48, 983, 212, 95, 'n : α', 43)

    # All three RETURN productions shown can produce an integer.
    line('M430 773V979', width=4)
    line('M430 810H334V843', gray=True, width=2.8)
    line('M430 810H499V843', gray=True, width=2.8)
    box(282, 847, 104, 58, 'CASE', 27, gray=True)
    box(446, 847, 106, 58, 'coalesce', 21, gray=True)
    box(324, 983, 212, 95, 'COUNT', 45)

    # One reserved result-table interface remains below the chosen COUNT.
    line('M430 1081V1246', width=4)
    parts.append(f'<rect x="393" y="1250" width="74" height="74" fill="white" '
                 f'stroke="{INK}" stroke-width="3.5"/>')
    parts.append('</svg>')
    svg = '\n'.join(parts) + '\n'
    ElementTree.fromstring(svg)
    path = ROOT / 'Random_AST_choice_branches.svg'
    path.write_text(svg, encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()

"""Redraw the supplied Overview as an editable, six-stage SVG.

Preserves the original five-region layout and black/green/red visual language.
Blue marks the three newly explicit method components; no raster is embedded.
"""

from html import escape
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
BLACK = "#171717"
GRAY = "#555555"
GREEN = "#307B59"
RED = "#B6403D"
BLUE = "#2B6CB0"
FONT = "Arial,Helvetica,sans-serif"
MIN_TEXT = 37  # About 6.1 pt when the complete 3080-unit figure is 180 mm wide.


class Canvas:
    def __init__(self):
        self.parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="3080" height="1720" '
                      'viewBox="0 0 3080 1720" role="img" aria-labelledby="title desc">',
                      '<title id="title">GraphSymPerf Overview revised to the six-stage method</title>',
                      '<desc id="desc">Random typed AST generation precedes local Pattern embedding; '
                      'the embedded AST and Pattern premises jointly drive graph-state construction. '
                      'Accepted bindings instantiate a full base query, one Pattern-defined mutation '
                      'creates its counterpart, and dependency checking precedes runtime validation.</desc>',
                      '<defs>',
                      '<marker id="arrow" viewBox="0 0 16 16" refX="14" refY="8" '
                      'markerWidth="22" markerHeight="22" markerUnits="userSpaceOnUse" orient="auto">'
                      f'<path d="M0 0L16 8L0 16Z" fill="{BLACK}"/></marker>',
                      '<marker id="feedback-arrow" viewBox="0 0 12 12" refX="10" refY="6" '
                      'markerWidth="17" markerHeight="17" markerUnits="userSpaceOnUse" orient="auto">'
                      f'<path d="M0 0L12 6L0 12Z" fill="{GRAY}"/></marker>',
                      '</defs>', '<rect width="3080" height="1720" fill="white"/>']

    def text(self, x, y, text, size=34, color=BLACK, weight=400, anchor="start", family=FONT, italic=False):
        size = max(size, MIN_TEXT)
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
                          f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}"'
                          + (' font-style="italic"' if italic else '') + f'>{escape(text)}</text>')

    def lines(self, x, y, lines, size=34, gap=45, **kwargs):
        for index, line in enumerate(lines):
            self.text(x, y + index * gap, line, size, **kwargs)

    def path(self, data, color=BLACK, width=5, arrow=False, dash=False):
        self.parts.append(f'<path d="{data}" fill="none" stroke="{color}" stroke-width="{width}" '
                          'stroke-linecap="round" stroke-linejoin="round"'
                          + (f' marker-end="url(#{"feedback-arrow" if dash else "arrow"})"' if arrow else '')
                          + (' stroke-dasharray="11 9"' if dash else '') + '/>')

    def rect(self, x, y, width, height, *, color=BLACK, fill="white", rounded=0, dash=False, stroke=3):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
                          f'rx="{rounded}" fill="{fill}" stroke="{color}" stroke-width="{stroke}"'
                          + (' stroke-dasharray="13 9"' if dash else '') + '/>')

    def circle(self, x, y, radius, color=BLACK, fill="white", width=4):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" '
                          f'stroke="{color}" stroke-width="{width}"/>')

    def heading(self, x, y, number, text, size=49, color=BLACK):
        self.circle(x + 38, y, 37, color, width=4)
        self.text(x + 38, y + 17, str(number), 52, color, 400, "middle")
        self.text(x + 91, y + 17, text, size, color, 750)

    def dot(self, x, y):
        self.circle(x, y, 6, BLACK, BLACK, width=1)

    def paper(self, x, y, label, width=140, height=180, color=BLACK, label_offset=None, label_size=64):
        fold = 37
        self.parts.append(f'<path d="M{x} {y}H{x + width - fold}L{x + width} {y + fold}'
                          f'V{y + height}H{x}Z" fill="white" stroke="{color}" stroke-width="5"/>')
        self.path(f'M{x + width - fold} {y}V{y + fold}H{x + width}', color, width=5)
        self.text(x + width / 2, y + (height * .66 if label_offset is None else label_offset),
                  label, label_size, color, 650, "middle", "Georgia,Times New Roman,serif", True)

    def save(self):
        content = '\n'.join([*self.parts, '</svg>']) + '\n'
        ElementTree.fromstring(content)
        destination = ROOT / 'Overview_six_stage.svg'
        destination.write_text(content, encoding='utf-8')
        print(destination)


def main():
    c = Canvas()

    # The new M1 occupies the upper half of the old skeleton region.
    c.heading(60, 55, 1, 'Random AST Generation', size=47, color=BLUE)
    c.rect(60, 105, 880, 260, color=BLUE, dash=True)
    c.text(105, 155, 'Query skeleton + typed recursive expansion', 33, weight=650)
    c.rect(380, 179, 160, 47, rounded=8)
    c.text(460, 213, 'Query', 30, weight=650, anchor='middle')
    c.text(105, 215, 'e.g.', 37, GRAY)
    c.rect(155, 250, 170, 50, rounded=8)
    c.text(240, 286, 'MATCH', 31, weight=650, anchor='middle')
    c.rect(540, 250, 190, 50, rounded=8)
    c.text(635, 286, 'RETURN', 31, weight=650, anchor='middle')
    c.rect(790, 250, 90, 50, color=BLUE, rounded=8, fill='#F5F9FE')
    c.text(835, 286, '□', 37, BLUE, 650, 'middle')
    c.path('M460 228V239H240V248', width=3)
    c.path('M460 239H635V248', width=3)
    c.path('M732 275H778', width=3, arrow=True)
    c.text(105, 350, 'Type · variable scope · structure budget', 26, GRAY)
    c.text(915, 350, 'A₀', 32, BLUE, 650, 'end')

    # M2 retains the original short-circuit / symmetry examples, now local laws.
    c.parts.append('<g transform="translate(0,35)">')
    c.heading(60, 395, 2, 'Pattern Embedding', size=49)
    c.rect(60, 440, 880, 450, dash=True)
    c.text(100, 490, 'Short-circuit patterns', 39, weight=750)
    c.text(115, 545, 'COALESCE(NULL, κ,', 33, family='Courier New,monospace')
    c.text(535, 545, 'L', 35, GREEN, 750, family='Courier New,monospace')
    c.text(570, 545, ')', 33, family='Courier New,monospace')
    c.path('M350 555V584', width=7, arrow=True)
    c.text(115, 628, 'COALESCE(NULL, κ,', 33, family='Courier New,monospace')
    c.text(535, 628, 'H', 35, RED, 750, family='Courier New,monospace')
    c.text(570, 628, ')', 33, family='Courier New,monospace')
    c.text(115, 676, 'Premise: κ is non-NULL', 27, GRAY)
    c.path('M100 690H665', color='#CFCFCF', width=2)
    c.text(100, 735, 'Symmetry patterns', 39, weight=750)
    c.path('M211 797H307', width=4)
    c.circle(180, 797, 30, GREEN)
    c.circle(338, 797, 30, RED)
    c.text(180, 810, 'L', 38, GREEN, anchor='middle')
    c.text(338, 810, 'H', 38, RED, anchor='middle')
    c.path('M388 797H438', width=8, arrow=True)
    c.path('M438 797H388', width=8, arrow=True)
    c.path('M516 797H600', width=4)
    c.circle(484, 797, 30, RED)
    c.circle(632, 797, 30, GREEN)
    c.text(484, 810, 'H', 38, RED, anchor='middle')
    c.text(632, 810, 'L', 38, GREEN, anchor='middle')
    c.text(415, 856, 'reorder under checked premises', 26, GRAY, anchor='middle')
    c.paper(750, 480, 'A', 135, 165)
    c.text(815, 724, 'Embedded', 30, weight=650, anchor='middle')
    c.text(815, 763, 'AST', 30, weight=650, anchor='middle')
    c.lines(815, 824, ['One Pattern', 'per typed slot'], 25, 44, color=GRAY, anchor='middle')
    c.path('M670 610H737', width=6, arrow=True)
    c.path('M815 337V465', width=6, arrow=True)
    c.text(837, 383, 'A₀', 31, BLUE)
    c.parts.append('</g>')

    # The old query-generation region now contains explicit M4 and M5.
    c.heading(1040, 55, 4, 'Query Instantiation', size=47, color=BLUE)
    c.heading(1885, 55, 5, 'Pattern Mutation', size=47)
    c.rect(1040, 105, 1900, 795, dash=True)
    c.text(1100, 170, 'Fill the full AST using accepted bindings', 32, GRAY)
    c.text(2210, 170, 'Apply one selected Pattern', 32, GRAY, anchor='middle')
    c.rect(1110, 465, 390, 190, color=BLUE, fill='#F5F9FE', rounded=20, stroke=4)
    c.text(1305, 525, 'Instantiate', 39, weight=750, anchor='middle')
    c.text(1305, 572, 'all terminals', 36, anchor='middle')
    c.text(1305, 623, 'A + (G, β, I)', 33, BLUE, 650, 'middle')
    c.path('M887 595H1095', width=7, arrow=True)
    c.text(995, 570, 'A', 37, weight=650, anchor='middle')
    c.path('M1503 560H1585', width=7, arrow=True)
    c.paper(1600, 475, 'Q₁', 140, 180)
    c.text(1670, 723, 'Base query', 39, weight=750, anchor='middle')
    c.rect(1900, 280, 620, 145, fill='#FAFAFA', rounded=22, stroke=4)
    c.text(2210, 368, 'Short-circuit mutation', 43, weight=750, anchor='middle')
    c.rect(1900, 655, 620, 145, fill='#FAFAFA', rounded=22, stroke=4)
    c.text(2210, 744, 'Symmetry mutation', 43, weight=750, anchor='middle')
    c.path('M1745 560H1810V352H1885', width=7, arrow=True)
    c.path('M1810 560V727H1885', width=7, arrow=True)
    c.dot(1810, 560)
    c.path('M2523 352H2585V560', width=7)
    c.path('M2523 727H2585V560H2670', width=7, arrow=True)
    c.dot(2585, 560)
    c.paper(2690, 420, 'Q₁', 140, 180, GRAY, label_offset=48, label_size=54)
    c.paper(2745, 480, 'Q₂', 140, 180)
    c.text(2780, 745, 'Query pair', 42, weight=750, anchor='middle')
    c.text(2210, 860, 'Same graph; one marked local transformation', 29, GRAY, anchor='middle')

    # Graph region preserved below the original skeleton region.
    c.heading(60, 1005, 3, 'Pattern-aware Graph State', size=44)
    c.rect(60, 1060, 880, 630, dash=True)
    c.text(100, 1118, 'Pattern premises + outer constraints', 34, weight=650)
    c.text(100, 1162, 'Witnesses + work-size requirements', 29, GRAY)
    c.rect(112, 1225, 90, 90, color=GREEN, rounded=12, stroke=4)
    c.text(157, 1290, 'L', 59, GREEN, 650, 'middle')
    c.lines(94, 1360, ['Low potential', 'work'], 32, 40, color=GREEN)
    c.rect(112, 1457, 90, 90, color=RED, rounded=12, stroke=4)
    c.text(157, 1522, 'H', 59, RED, 650, 'middle')
    c.lines(94, 1592, ['High potential', 'work'], 32, 40, color=RED)
    c.path('M305 1400H408', width=9, arrow=True)
    c.path('M450 1400L580 1270', GREEN, width=5)
    c.path('M450 1400L590 1460', RED, width=5)
    for y in (1375, 1460, 1545):
        c.path(f'M590 1460L790 {y}', RED, width=5)
    c.circle(450, 1400, 31)
    c.circle(580, 1270, 31, GREEN)
    c.circle(590, 1460, 31, RED)
    for y in (1375, 1460, 1545):
        c.circle(790, y, 31, RED)
    c.text(580, 1217, 'Few', 39, GREEN, 650, 'middle')
    c.text(790, 1317, 'Many', 39, RED, 650, 'middle')
    c.text(851, 1510, '⋮', 49, RED)
    c.text(560, 1610, 'Graph state: G, β, I', 38, weight=750, anchor='middle')
    c.text(560, 1660, 'graph · bindings · facts', 37, GRAY, anchor='middle')
    c.path('M885 927V1045', width=7, arrow=True)

    # State/binding feedback to instantiation stays in the inter-panel gutter.
    c.path('M943 1180H995V825H1305V672', width=6, arrow=True)
    c.rect(1060, 787, 210, 70, color='white', fill='white', stroke=0)
    c.text(1165, 837, '(G, β, I)', 32, weight=650, anchor='middle')

    # The new dependency gate separates construction from runtime checking.
    c.heading(1040, 1005, 6, 'Dependency Checking & Propagation', size=47, color=BLUE)
    c.rect(1040, 1060, 1900, 190, color=BLUE, fill='#F8FBFE', dash=True)
    c.text(1100, 1118, 'Result relation: full local boundary → root', 36, weight=650)
    c.text(1100, 1170, 'Outer demand · input multiplicity · shared reads', 34)
    c.text(1100, 1222, 'Invalid target / unknown relation → bounded resampling', 28, GRAY)
    c.rect(2695, 1095, 190, 92, color=GREEN, fill='white', rounded=12, stroke=4)
    c.text(2790, 1156, 'PASS', 42, GREEN, 750, 'middle')
    c.path('M2790 778V1045', width=8, arrow=True)
    c.text(2820, 967, '(Q₁, Q₂)', 32)
    c.path('M943 1225H1025', width=5, arrow=True)
    c.text(990, 1270, 'G, I', 27, GRAY, anchor='middle')

    # Runtime verification remains in the same two bottom-right panels.
    c.text(1040, 1326, 'Execution', 49, weight=750)
    c.rect(1040, 1370, 520, 320, dash=True)
    c.path('M2790 1192V1280H1480V1355', width=8, arrow=True)
    c.path('M943 1510H1025', width=8, arrow=True)
    c.text(990, 1485, 'G', 35, weight=650, anchor='middle')
    c.rect(1155, 1435, 190, 148, stroke=5)
    c.parts.append(f'<ellipse cx="1250" cy="1435" rx="95" ry="27" fill="white" stroke="{BLACK}" stroke-width="5"/>')
    c.path('M1155 1583C1155 1620 1345 1620 1345 1583', width=5)
    c.path('M1155 1505C1155 1542 1345 1542 1345 1505', width=4)
    c.rect(1290, 1517, 110, 100, rounded=3, stroke=5)
    c.path('M1312 1542L1345 1588L1378 1542', width=4)
    for x, y in ((1312, 1542), (1345, 1588), (1378, 1542)):
        c.circle(x, y, 7, BLACK, BLACK, width=1)
    c.text(1300, 1660, 'Graph DBMS', 41, weight=750, anchor='middle')

    c.text(1640, 1326, 'Result & Performance Validation', 48, weight=750)
    c.rect(1640, 1370, 1300, 320, dash=True)
    c.path('M1565 1510H1625', width=8, arrow=True)
    # Typed result bags retain the familiar stacked-table icon.
    c.rect(1735, 1420, 120, 120, color=GRAY, stroke=5)
    c.rect(1760, 1445, 120, 120, stroke=5)
    for y in (1475, 1505, 1535):
        c.path(f'M1760 {y}H1880', width=3)
    c.path('M1800 1445V1565', width=3)
    c.lines(1810, 1620, ['Equal result', 'bags'], 37, 42, weight=750, anchor='middle')
    c.path('M1940 1500H2075', width=9, arrow=True)
    # Stopwatch preserved from the original illustration.
    c.circle(2245, 1490, 69, width=5)
    c.path('M2245 1420V1394M2222 1394H2268M2293 1437L2312 1418', width=5)
    c.path('M2245 1444V1491L2283 1511', width=5)
    c.path('M2327 1477C2345 1550 2297 1585 2252 1577', width=5, arrow=True)
    c.lines(2245, 1620, ['Stable', 'slowdown'], 37, 42, weight=750, anchor='middle')
    c.path('M2380 1500H2515', width=9, arrow=True)
    # The bug silhouette now denotes a candidate, not a confirmed root cause.
    c.parts.append(f'<path d="M2668 1449Q2668 1427 2690 1427H2766Q2788 1427 2788 1449V1516'
                   f'Q2788 1578 2728 1578Q2668 1578 2668 1516Z" fill="{BLACK}"/>')
    c.parts.append(f'<path d="M2695 1412A33 33 0 0 1 2761 1412Z" fill="{BLACK}"/>')
    for data in ('M2677 1455L2645 1423', 'M2779 1455L2811 1423',
                 'M2670 1500H2631', 'M2786 1500H2825',
                 'M2682 1541L2648 1577', 'M2774 1541L2808 1577'):
        c.path(data, width=14)
    c.path('M2728 1471V1573', color='white', width=10)
    c.lines(2728, 1620, ['Performance', 'candidate'], 38, 42, color=RED, weight=750, anchor='middle')

    # Thin feedback does not cross titles or any active forward flow.
    c.path('M2943 1220H3010V8H975V220H947', color=GRAY, width=3, arrow=True, dash=True)
    c.save()


if __name__ == '__main__':
    main()

"""Render the same M2 pseudocode as an editable SVG and a LaTeX algorithm."""

from html import escape
from pathlib import Path
from xml.etree import ElementTree


REPOSITORY = Path(__file__).resolve().parents[2]
PICTURE = REPOSITORY / 'picture'
TEX = REPOSITORY / 'docs' / 'algorithms' / 'pattern-embedding' / 'algorithm.tex'
SERIF = 'Times New Roman,Times,serif'


def piece(value, kind='math'):
    return value, kind


ROWS = [
    (0, [piece('slot ← '), piece('FindHole', 'proc'), piece('(ast)')],
     'The hole reserved by M1', r'\State \textit{slot} $\gets$ \Call{FindHole}{\textit{ast}}\Comment{The hole reserved by M1}'),
    (0, [piece('if not ', 'keyword'), piece('Compatible', 'proc'), piece('(slot, pattern)'), piece(' then', 'keyword')],
     'Check interface and output type', r'\If{not \Call{Compatible}{\textit{slot}, \textit{pattern}}}\Comment{Check interface and output type}'),
    (1, [piece('return ', 'keyword'), piece('Reject', 'plain')], '', r'\State \Return \textsc{Reject}'),
    (0, [piece('end if', 'keyword')], '', r'\EndIf'),
    (0, [piece('fragment ← '), piece('ExpandPattern', 'proc'), piece('(pattern, slot)')],
     'Choose the local structure', r'\State \textit{fragment} $\gets$ \Call{ExpandPattern}{\textit{pattern}, \textit{slot}}\Comment{Choose the local structure}'),
    (0, [piece('Rename private variables in ', 'plain'), piece('fragment'), piece(' to avoid name conflicts', 'plain')],
     '', r'\State Rename private variables in \textit{fragment} to avoid name conflicts'),
    (0, [piece('tree ← '), piece('FillHole', 'proc'), piece('(ast, slot, fragment)')],
     'Preserve the outer context', r'\State \textit{tree} $\gets$ \Call{FillHole}{\textit{ast}, \textit{slot}, \textit{fragment}}\Comment{Preserve the outer context}'),
    (0, [piece('Mark the mutation site inside the inserted ', 'plain'), piece('fragment')],
     '', r'\State Mark the mutation site inside the inserted \textit{fragment}'),
    (0, [piece('Save the mutation rule and its full enclosing context', 'plain')],
     'Used later in M5 and M6', r'\State Save the mutation rule and its full enclosing context\Comment{Used later in M5 and M6}'),
    (0, [piece('Save the pattern conditions for graph construction', 'plain')],
     'Checked later in M3', r'\State Save the pattern conditions for graph construction\Comment{Checked later in M3}'),
    (0, [piece('if not ', 'keyword'), piece('CheckEmbedding', 'proc'), piece('(ast, tree, slot)'), piece(' then', 'keyword')],
     'Types, scope, and insertion range', r'\If{not \Call{CheckEmbedding}{\textit{ast}, \textit{tree}, \textit{slot}}}\Comment{Types, scope, and insertion range}'),
    (1, [piece('return ', 'keyword'), piece('Reject', 'plain')], '', r'\State \Return \textsc{Reject}'),
    (0, [piece('end if', 'keyword')], '', r'\EndIf'),
    (0, [piece('Freeze the structure except for the saved mutation', 'plain')],
     'Keep terminal placeholders', r'\State Freeze the structure except for the saved mutation\Comment{Keep terminal placeholders}'),
    (0, [piece('return ', 'keyword'), piece('tree')], '', r'\State \Return \textit{tree}'),
]


def rich_text(x, y, fragments, size=36, anchor='start'):
    result = [f'<text x="{x}" y="{y}" font-family="{SERIF}" font-size="{size}" '
              f'text-anchor="{anchor}" fill="black" xml:space="preserve">']
    for value, kind in fragments:
        style = (' font-weight="700"' if kind == 'keyword' else
                 ' style="font-variant:small-caps"' if kind == 'proc' else
                 ' font-style="italic"' if kind == 'math' else '')
        result.append(f'<tspan{style}>{escape(value)}</tspan>')
    return ''.join([*result, '</text>'])


def main():
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1020" '
        'viewBox="0 0 1800 1020" role="img" aria-labelledby="title desc">',
        '<title id="title">Algorithm 2: Pattern embedding into an AST</title>',
        '<desc id="desc">Fifteen readable steps find a slot, expand a local Pattern, rename variables, '
        'insert the fragment, mark the mutation site and save its complete context, '
        'validate structure, and freeze all undeclared generation changes. Graph premises are '
        'recorded for M3 rather than checked in M2.</desc>',
        '<rect width="1800" height="1020" fill="white"/>',
        '<path d="M28 18H1772" stroke="black" stroke-width="3.5"/>',
        rich_text(28, 73, [piece('Algorithm 2 ', 'keyword'), piece('Pattern embedding into an AST', 'plain')], 46),
        '<path d="M28 99H1772" stroke="black" stroke-width="1.7"/>',
        rich_text(28, 154, [piece('Input: ', 'keyword'), piece('Abstract AST ', 'plain'), piece('ast'),
                            piece(' with one typed hole; predefined Pattern ', 'plain'), piece('pattern')], 37),
        rich_text(28, 204, [piece('Output: ', 'keyword'), piece('AST ', 'plain'), piece('tree'),
                            piece(' with the Pattern embedded, or rejection', 'plain')], 37),
    ]
    for index, (indent, fragments, comment, _) in enumerate(ROWS, 1):
        y = 262 + (index - 1) * 50
        elements.append(rich_text(78, y, [piece(f'{index}:', 'plain')], 33, 'end'))
        elements.append(rich_text(100 + indent * 55, y, fragments))
        if comment:
            elements.append(rich_text(1770, y, [piece('▷ ' + comment, 'plain')], 33, 'end'))
    elements.extend(['<path d="M28 997H1772" stroke="black" stroke-width="2.2"/>', '</svg>'])
    svg = '\n'.join(elements) + '\n'
    ElementTree.fromstring(svg)
    output = PICTURE / 'Algorithm_2_pattern_embedding.svg'
    output.write_text(svg, encoding='utf-8')

    tex = [r'\begin{algorithm}[H]', r'\caption{Pattern embedding into an AST}', r'\label{alg:pattern-embedding}',
           r'\begin{algorithmic}[1]',
           r'\Require Abstract AST \textit{ast} with one typed hole; predefined Pattern \textit{pattern}',
           r'\Ensure AST \textit{tree} with the Pattern embedded, or rejection']
    tex.extend(row[3] for row in ROWS)
    tex.extend([r'\end{algorithmic}', r'\end{algorithm}'])
    TEX.write_text('\n'.join(tex) + '\n', encoding='utf-8')
    print(output)
    print(TEX)


if __name__ == '__main__':
    main()

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
    (0, [piece('if ', 'keyword'), piece('¬'), piece('Compatible', 'proc'), piece('(h, p)'), piece(' then', 'keyword')],
     'Interface categories and types', r'\If{$\neg$\Call{Compatible}{$h,p$}}\Comment{Interface categories and types}'),
    (1, [piece('return ', 'keyword'), piece('Reject', 'plain')],
     'Incompatible Pattern', r'\State \Return \textsc{Reject}\Comment{Incompatible Pattern}'),
    (0, [piece('end if', 'keyword')], '', r'\EndIf'),
    (0, [piece('(K, τ, Φ, σ, ε) ← '), piece('Expand', 'proc'), piece('(p, h)')],
     'Local source, rewrite, and premises', r'\State $(K,\tau,\Phi,\sigma,\epsilon)\gets$\Call{Expand}{$p,h$}\Comment{Local source, rewrite, and premises}'),
    (0, [piece('(K, τ, Φ) ← '), piece('AlphaRename', 'proc'), piece('(K, τ, Φ, '), piece('Vars', 'proc'), piece('(A₀))')],
     'Avoid capture; retain imports', r'\State $(K,\tau,\Phi)\gets$\Call{AlphaRename}{$K,\tau,\Phi,\text{\textsc{Vars}}(A_0)$}\Comment{Avoid capture; retain imports}'),
    (0, [piece('A ← '), piece('Replace', 'proc'), piece('(A₀, h, K)')],
     'Preserve the outer context', r'\State $A\gets$\Call{Replace}{$A_0,h,K$}\Comment{Preserve the outer context}'),
    (0, [piece('s ← '), piece('Relocate', 'proc'), piece('(h, σ)')],
     'Actual mutation site', r'\State $s\gets$\Call{Relocate}{$h,\sigma$}\Comment{Actual mutation site}'),
    (0, [piece('e ← '), piece('Relocate', 'proc'), piece('(h, ε)')],
     'Complete semantic boundary', r'\State $e\gets$\Call{Relocate}{$h,\epsilon$}\Comment{Complete semantic boundary}'),
    (0, [piece('M ← '), piece('Register', 'proc'), piece('(p, s, e, τ, Φ)')],
     'Premises passed to M3', r'\State $M\gets$\Call{Register}{$p,s,e,\tau,\Phi$}\Comment{Premises passed to M3}'),
    (0, [piece('if ', 'keyword'), piece('¬'), piece('ValidateEmbedding', 'proc'), piece('(A₀, A, h, M)'), piece(' then', 'keyword')],
     'Types, scope, and unchanged host', r'\If{$\neg$\Call{ValidateEmbedding}{$A_0,A,h,M$}}\Comment{Types, scope, and unchanged host}'),
    (1, [piece('return ', 'keyword'), piece('Reject', 'plain')],
     'Invalid embedding', r'\State \Return \textsc{Reject}\Comment{Invalid embedding}'),
    (0, [piece('end if', 'keyword')], '', r'\EndIf'),
    (0, [piece('FreezeStructure', 'proc'), piece('(A, M)')],
     'Only declared structural rewrites', r'\State \Call{FreezeStructure}{$A,M$}\Comment{Only declared structural rewrites}'),
    (0, [piece('return ', 'keyword'), piece('(A, s, M)')],
     'Symbols remain unbound', r'\State \Return $(A,s,M)$\Comment{Symbols remain unbound}'),
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
        '<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="970" '
        'viewBox="0 0 1800 970" role="img" aria-labelledby="title desc">',
        '<title id="title">Algorithm 2: Pattern embedding into an AST</title>',
        '<desc id="desc">Fourteen numbered steps check a typed hole, expand and alpha-rename '
        'a local Pattern, insert its source, register its mutation and complete semantic boundary, '
        'validate structure, and freeze all undeclared generation changes. Graph premises are '
        'recorded for M3 rather than checked in M2.</desc>',
        '<rect width="1800" height="970" fill="white"/>',
        '<path d="M28 18H1772" stroke="black" stroke-width="3.5"/>',
        rich_text(28, 73, [piece('Algorithm 2 ', 'keyword'), piece('Pattern embedding into an AST', 'plain')], 46),
        '<path d="M28 99H1772" stroke="black" stroke-width="1.7"/>',
        rich_text(28, 154, [piece('Require: ', 'keyword'), piece('Abstract AST ', 'plain'), piece('A₀'),
                            piece(', typed hole ', 'plain'), piece('h'), piece(', Pattern ', 'plain'), piece('p')], 37),
        rich_text(28, 204, [piece('Ensure: ', 'keyword'), piece('Embedded AST ', 'plain'), piece('A'),
                            piece(', mutation site ', 'plain'), piece('s'), piece(', record ', 'plain'), piece('M'),
                            piece(', or rejection', 'plain')], 37),
    ]
    for index, (indent, fragments, comment, _) in enumerate(ROWS, 1):
        y = 262 + (index - 1) * 50
        elements.append(rich_text(78, y, [piece(f'{index}:', 'plain')], 33, 'end'))
        elements.append(rich_text(100 + indent * 55, y, fragments))
        if comment:
            elements.append(rich_text(1770, y, [piece('▷ ' + comment, 'plain')], 33, 'end'))
    elements.extend(['<path d="M28 947H1772" stroke="black" stroke-width="2.2"/>', '</svg>'])
    svg = '\n'.join(elements) + '\n'
    ElementTree.fromstring(svg)
    output = PICTURE / 'Algorithm_2_pattern_embedding.svg'
    output.write_text(svg, encoding='utf-8')

    tex = [r'\begin{algorithm}[H]', r'\caption{Pattern embedding into an AST}', r'\label{alg:pattern-embedding}',
           r'\begin{algorithmic}[1]',
           r'\Require Abstract AST $A_0$, typed hole $h$, Pattern $p$',
           r'\Ensure Embedded AST $A$, mutation site $s$, record $M$, or rejection']
    tex.extend(row[3] for row in ROWS)
    tex.extend([r'\end{algorithmic}', r'\end{algorithm}'])
    TEX.write_text('\n'.join(tex) + '\n', encoding='utf-8')
    print(output)
    print(TEX)


if __name__ == '__main__':
    main()

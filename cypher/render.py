import json
from data_structures.query import Query as ExecutableQuery
from .ast_nodes import *


def render(tree):
    # Rendering changes syntax only; pattern transformations happen on the AST.
    return ExecutableQuery(_render(tree), columns=tuple(variable.name for variable in tree.output()))


def identifier(value):
    return chr(96) + value.replace(chr(96), chr(96) * 2) + chr(96)


def literal(value):
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def _render(tree):
    match tree:
        case NodePattern():
            labels = ''.join((':' + identifier(label) for label in tree.labels))
            props = ', '.join((identifier(key) + ': ' + _render(value) for key, value in tree.properties))
            return f'({tree.variable.name}{labels}' + (' {' + props + '}' if props else '') + ')'
        case RelationshipPattern():
            name = tree.variable.name if tree.variable else ''
            types = ':' + '|'.join((identifier(value) for value in tree.types)) if tree.types else ''
            length = f'*{tree.minimum}..{tree.maximum}' if (tree.minimum, tree.maximum) != (1, 1) else ''
            middle = f'[{name}{types}{length}]'
            return {'out': '-' + middle + '->', 'in': '<-' + middle + '-', 'both': '-' + middle + '-'}[tree.direction]
        case PathPattern():
            text = _render(tree.nodes[0])
            for relationship, node in zip(tree.relationships, tree.nodes[1:]):
                text += _render(relationship) + _render(node)
            return (tree.variable.name + ' = ' if tree.variable else '') + text
        case Literal():
            return literal(tree.value)
        case Reference():
            return tree.variable.name
        case Property():
            return f'{tree.variable.name}.{identifier(tree.key)}'
        case Unary():
            expression = _render(tree.operand)
            return f'({expression} {tree.operator})' if tree.operator.startswith('IS ') else f'(NOT {expression})'
        case Binary():
            return f'({_render(tree.left)} {tree.operator} {_render(tree.right)})'
        case Function():
            args = ', '.join((_render(value) for value in tree.arguments))
            distinct = "DISTINCT " if tree.distinct else ""
            return f"{tree.name}({distinct}{args})"
        case CaseExpression():
            branches = ' '.join((f'WHEN {_render(condition)} THEN {_render(value)}' for condition, value in tree.branches))
            return f'CASE {branches} ELSE {_render(tree.otherwise)} END'
        case ListExpression():
            return '[' + ', '.join((_render(item) for item in tree.items)) + ']'
        case ListIndex():
            return f'({_render(tree.source)})[{_render(tree.index)}]'
        case Quantifier():
            return f'{tree.name}({tree.variable.name} IN {_render(tree.source)} WHERE {_render(tree.predicate)})'
        case SubqueryExpression():
            return f'{tree.mode} {{\n{_render(tree.query)}\n}}'
        case MatchBody():
            text = "MATCH " + ", ".join(_render(path) for path in tree.patterns)
            return text + (" WHERE " + _render(tree.where) if tree.where else "")
        case PatternComprehension():
            return "[" + _render(tree.pattern) + " WHERE " + _render(tree.where) + " | " + _render(tree.projection) + "]"
        case Query():
            return '\n'.join((_render(clause) for clause in tree.clauses))
        case Union():
            separator = '\nUNION ALL\n' if tree.all else '\nUNION\n'
            # Preserve grouping when a conditional query occupies a UNION branch.
            rendered = [
                '{\n' + _render(branch) + '\n}' if isinstance(branch, ConditionalQuery) else _render(branch)
                for branch in tree.branches
            ]
            return separator.join(rendered)
        case ConditionalQuery():
            text = '\n'.join((f'WHEN {_render(condition)} THEN {{\n{_render(query)}\n}}' for condition, query in tree.branches))
            return text + '\nELSE {\n' + _render(tree.otherwise) + '\n}'
        case Match():
            text = ('OPTIONAL MATCH ' if tree.optional else 'MATCH ') + ', '.join((_render(path) for path in tree.paths))
            return text + ('\nWHERE ' + _render(tree.where) if tree.where else '')
        case ProjectionItem():
            return _render(tree.expression) + ' AS ' + tree.alias.name
        case Projection():
            text = tree.keyword + (' DISTINCT ' if tree.distinct else ' ') + ', '.join((_render(item) for item in tree.items))
            if tree.where:
                text += '\nWHERE ' + _render(tree.where)
            if tree.order:
                text += '\nORDER BY ' + ', '.join((_render(expr) + (' DESC' if descending else ' ASC') for expr, descending in tree.order))
            if tree.skip:
                text += '\nSKIP ' + _render(tree.skip)
            if tree.limit:
                text += '\nLIMIT ' + _render(tree.limit)
            return text
        case Call():
            return 'CALL (' + ', '.join((var.name for var in tree.imports)) + ') {\n' + _render(tree.query) + '\n}'
        case Unwind():
            return f'UNWIND {_render(tree.expression)} AS {tree.variable.name}'
        case _:
            raise TypeError(type(tree).__name__)

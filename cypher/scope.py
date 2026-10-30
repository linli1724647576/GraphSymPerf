from dataclasses import dataclass, field
from cypher.ast_nodes import *
from cypher.ast_nodes.expressions import Expression
from data_structures.case import Site


@dataclass
class Scope:
    # Most backends keep imports across WITH; ArcadeDB requires explicit projection.
    # 多数后端在 WITH 后保留导入变量，ArcadeDB 则要求显式投影。
    local: dict = field(default_factory=dict)
    imported: dict = field(default_factory=dict)
    persistent_imports: bool = True

    def visible(self):
        return self.imported | self.local

    def child(self):
        return Scope(imported=self.visible(), persistent_imports=self.persistent_imports)

    def add(self, variable):
        self.local[variable.name] = variable

    def project(self, variables):
        if not self.persistent_imports:
            self.imported = {}
        self.local = {var.name: var for var in variables if var.name not in self.imported}


def analyze(query, initial=None, persistent_imports=True):
    # Record the variables visible at each candidate site while checking references.
    # 检查变量引用的同时，记录各候选插入位置可见的变量。
    sites = []
    _query(query, Scope(imported=initial or {}, persistent_imports=persistent_imports), (), sites)
    return sites


def _query(query, scope, path, sites):
    sites.append(Site(path, query, scope.visible()))
    if isinstance(query, MatchBody):
        for i, pattern in enumerate(query.patterns):
            _pattern(pattern, scope, path + ("patterns", i), sites)
        if query.where:
            _expression(query.where, scope, path + ("where",), sites)
        return
    if isinstance(query, Union):
        expected = [(v.name, v.kind) for v in query.output()]
        for i, branch in enumerate(query.branches):
            assert [(v.name, v.kind) for v in branch.output()] == expected, "UNION output mismatch"
            _query(branch, scope.child(), path + ("branches", i), sites)
        return
    if isinstance(query, ConditionalQuery):
        expected = [(v.name, v.kind) for v in query.output()]
        for i, (condition, branch) in enumerate(query.branches):
            _expression(condition, scope, path + ("branches", i, 0), sites)
            assert condition.kind == "bool"
            assert [(v.name, v.kind) for v in branch.output()] == expected, "WHEN output mismatch"
            _query(branch, scope.child(), path + ("branches", i, 1), sites)
        _query(query.otherwise, scope.child(), path + ("otherwise",), sites)
        return
    assert query.clauses and isinstance(query.clauses[-1], Projection) and query.clauses[-1].keyword == "RETURN"
    for i, clause in enumerate(query.clauses):
        location = path + ("clauses", i)
        before = scope.visible()
        sites.append(Site(location, clause, before))
        if isinstance(clause, Match):
            # A replacement sees the input scope; WHERE also sees this MATCH's variables.
            # 匹配结构替换使用输入作用域；WHERE 还可以引用本 MATCH 引入的变量。
            for pattern in clause.paths:
                for variable in pattern.variables():
                    if variable.name not in scope.visible():
                        scope.add(variable)
            for j, pattern in enumerate(clause.paths):
                sites.append(Site(location + ("paths", j), pattern, before))
                for n, node in enumerate(pattern.nodes):
                    for k, (_, value) in enumerate(node.properties):
                        _expression(value, scope, location + ("paths", j, "nodes", n, "properties", k, 1), sites)
            if clause.where:
                _expression(clause.where, scope, location + ("where",), sites)
                assert clause.where.kind == "bool"
        elif isinstance(clause, Projection):
            for j, item in enumerate(clause.items):
                _expression(item.expression, scope, location + ("items", j, "expression"), sites)
                assert item.expression.kind == item.alias.kind, "Projection type mismatch"
            scope.project([item.alias for item in clause.items])
            if clause.where:
                _expression(clause.where, scope, location + ("where",), sites)
                assert clause.where.kind == "bool"
            for j, (expression, _) in enumerate(clause.order):
                _expression(expression, scope, location + ("order", j, 0), sites)
            for name in ("skip", "limit"):
                expression = getattr(clause, name)
                if expression:
                    assert isinstance(expression, Literal) and expression.kind == "int"
        elif isinstance(clause, Call):
            # CALL imports explicitly and exposes only its declared output columns.
            # CALL 显式导入变量，只把声明的返回列提供给外层。
            imported = {}
            for variable in clause.imports:
                assert variable.name in before, f"Undefined CALL import: {variable.name}"
                imported[variable.name] = variable
            _query(clause.query, Scope(imported=imported, persistent_imports=scope.persistent_imports), location + ("query",), sites)
            for variable in clause.query.output():
                assert variable.name not in before, f"CALL output shadows a variable: {variable.name}"
                scope.add(variable)
        elif isinstance(clause, Unwind):
            _expression(clause.expression, scope, location + ("expression",), sites)
            assert clause.expression.kind == "list_" + clause.variable.kind
            scope.add(clause.variable)
        else:
            raise TypeError(type(clause).__name__)


def _expression(expression, scope, path, sites):
    sites.append(Site(path, expression, scope.visible()))
    if isinstance(expression, (Reference, Property)):
        variable = expression.variable
        assert variable.name in scope.visible(), f"Undefined variable: {variable.name}"
        assert scope.visible()[variable.name].kind == variable.kind, f"Variable type mismatch: {variable.name}"
    elif isinstance(expression, PatternComprehension):
        # Imports remain visible; variables introduced by the pattern cannot escape the list.
        # 保留外层变量可见性，模式推导新引入的变量不能泄露到列表之外。
        child = scope.child()
        _pattern(expression.pattern, child, path + ("pattern",), sites)
        _expression(expression.where, child, path + ("where",), sites)
        _expression(expression.projection, child, path + ("projection",), sites)
        assert expression.where.kind == "bool"
    elif isinstance(expression, SubqueryExpression):
        _query(expression.query, scope.child(), path + ("query",), sites)
        if expression.mode == "COLLECT":
            assert len(expression.query.output()) == 1
    elif isinstance(expression, Quantifier):
        # The iteration variable exists only inside the quantifier predicate.
        # 迭代变量只在量词谓词内部可见，不能泄露到外层表达式。
        _expression(expression.source, scope, path + ("source",), sites)
        child = scope.child()
        child.add(expression.variable)
        assert expression.source.kind == "list_" + expression.variable.kind
        _expression(expression.predicate, child, path + ("predicate",), sites)
    elif isinstance(expression, ListIndex):
        _expression(expression.source, scope, path + ("source",), sites)
        _expression(expression.index, scope, path + ("index",), sites)
        assert expression.source.kind.startswith("list_") and expression.index.kind == "int"
    elif isinstance(expression, Unary):
        _expression(expression.operand, scope, path + ("operand",), sites)
    elif isinstance(expression, Binary):
        _expression(expression.left, scope, path + ("left",), sites)
        _expression(expression.right, scope, path + ("right",), sites)
        if expression.operator in {"AND", "OR"}:
            assert expression.left.kind == expression.right.kind == "bool"
    elif isinstance(expression, Function):
        for i, value in enumerate(expression.arguments):
            _expression(value, scope, path + ("arguments", i), sites)
    elif isinstance(expression, CaseExpression):
        for i, (condition, value) in enumerate(expression.branches):
            _expression(condition, scope, path + ("branches", i, 0), sites)
            _expression(value, scope, path + ("branches", i, 1), sites)
            assert condition.kind == "bool" and value.kind == expression.kind
        _expression(expression.otherwise, scope, path + ("otherwise",), sites)
    elif isinstance(expression, ListExpression):
        for i, value in enumerate(expression.items):
            _expression(value, scope, path + ("items", i), sites)
    else:
        assert isinstance(expression, Literal), type(expression).__name__


def _pattern(pattern, scope, path, sites):
    for variable in pattern.variables():
        scope.add(variable)
    for i, node in enumerate(pattern.nodes):
        for j, (_, expression) in enumerate(node.properties):
            _expression(expression, scope, path + ("nodes", i, "properties", j, 1), sites)

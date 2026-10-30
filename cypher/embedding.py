from cypher.ast_nodes import *
from data_structures.ast import walk
from cypher.scope import analyze
from data_structures.case import Inapplicable
from .render import render


def choose_site(query, pattern, rng, subquery_call=True, subquery_unwind=True, persistent_imports=True):
    sites = [site for site in analyze(query, persistent_imports=persistent_imports) if pattern.accepts(site)]
    if not sites:
        return None
    # Exclude sites inside collect arguments, where replacing a constant could expose order.
    # 排除 collect 参数内部的位置，避免替换常量后引入可观察的顺序差异。
    ordered_lists = [path for path, node in walk(query) if isinstance(node, Function) and node.name == "collect"]
    sites = [site for site in sites if not any(site.path[:len(path)] == path for path in ordered_lists)]
    if not subquery_unwind and pattern.id in {"S1", "S2", "S3", "S4", "Y1", "Y2"}:
        # These backends reject subquery work items directly inside UNWIND expressions.
        # 这些后端不接受直接位于 UNWIND 表达式内部的子查询工作项。
        unwinds = [path for path, node in walk(query) if isinstance(node, Unwind)]
        sites = [site for site in sites if not any(site.path[:len(path)] == path for path in unwinds)]
    if pattern.id == "S6" and not subquery_call:
        # The bounded variant introduces CALL; Memgraph forbids it inside expression subqueries.
        # 有限需求变体引入 CALL，Memgraph 不允许它出现在表达式子查询内部。
        expressions = [path for path, node in walk(query) if isinstance(node, SubqueryExpression)]
        sites = [site for site in sites if not any(site.path[:len(path)] == path for path in expressions)]
    return rng.choice(sites) if sites else None


def check_pair(source, mutant, case, persistent_imports=True):
    # These are structural checks; complete result equality is checked on the database.
    # 此处只检查结构和输出接口；完整结果是否相同由数据库执行后比较。
    analyze(source, persistent_imports=persistent_imports)
    analyze(mutant, persistent_imports=persistent_imports)
    if render(source).text == render(mutant).text:
        raise Inapplicable("Mutation did not change the query text")
    source_columns = [(v.name, v.kind) for v in source.output()]
    mutant_columns = [(v.name, v.kind) for v in mutant.output()]
    assert source_columns == mutant_columns, "Mutation changed the output interface"

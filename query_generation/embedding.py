from ast_nodes import *
from ast_nodes.base import walk
from data_structures.scope import analyze
from data_structures.case import Inapplicable


def choose_site(query, pattern, rng):
    sites = [site for site in analyze(query) if pattern.accepts(site)]
    if not sites:
        return None
    # Exclude sites inside collect arguments, where replacing a constant could expose order.
    # 排除 collect 参数内部的位置，避免替换常量后引入可观察的顺序差异。
    ordered_lists = [path for path, node in walk(query) if isinstance(node, Function) and node.name == "collect"]
    sites = [site for site in sites if not any(site.path[:len(path)] == path for path in ordered_lists)]
    return rng.choice(sites) if sites else None


def check_pair(source, mutant, case):
    # These are structural checks; complete result equality is checked on the database.
    # 此处只检查结构和输出接口；完整结果是否相同由数据库执行后比较。
    analyze(source)
    analyze(mutant)
    if source.to_cypher() == mutant.to_cypher():
        raise Inapplicable("Mutation did not change the query text")
    source_columns = [(v.name, v.kind) for v in source.output()]
    mutant_columns = [(v.name, v.kind) for v in mutant.output()]
    assert source_columns == mutant_columns, "Mutation changed the output interface"

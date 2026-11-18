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
    ordered_lists = [path for path, node in walk(query) if isinstance(node, Function) and node.name == "collect"]
    sites = [site for site in sites if not any(site.path[:len(path)] == path for path in ordered_lists)]
    if not subquery_unwind and pattern.id in {"SC1", "SC2", "SC3", "SC4", "SY1", "SY2"}:
        # These backends reject subquery work items directly inside UNWIND expressions.
        unwinds = [path for path, node in walk(query) if isinstance(node, Unwind)]
        sites = [site for site in sites if not any(site.path[:len(path)] == path for path in unwinds)]
    if pattern.id == "SC6" and not subquery_call:
        # The bounded variant introduces CALL; Memgraph forbids it inside expression subqueries.
        expressions = [path for path, node in walk(query) if isinstance(node, SubqueryExpression)]
        sites = [site for site in sites if not any(site.path[:len(path)] == path for path in expressions)]
    return rng.choice(sites) if sites else None


def optional_before_match(query):
    """True if some clause list has a mandatory MATCH after an OPTIONAL MATCH."""
    for _, node in walk(query):
        seen_optional = False
        for clause in getattr(node, "clauses", None) or []:
            if isinstance(clause, Match):
                if clause.optional:
                    seen_optional = True
                elif seen_optional:
                    return True
    return False


def check_pair(source, mutant, case, persistent_imports=True, match_after_optional=True):
    # These are structural checks; complete result equality is checked on the database.
    analyze(source, persistent_imports=persistent_imports)
    analyze(mutant, persistent_imports=persistent_imports)
    if not match_after_optional and (optional_before_match(source) or optional_before_match(mutant)):
        # The seed or the embedded form puts MATCH after OPTIONAL MATCH, which the target rejects.
        raise Inapplicable("MATCH after OPTIONAL MATCH is unsupported by the target")
    if render(source).text == render(mutant).text:
        raise Inapplicable("Mutation did not change the query text")
    source_columns = [(v.name, v.kind) for v in source.output()]
    mutant_columns = [(v.name, v.kind) for v in mutant.output()]
    assert source_columns == mutant_columns, "Mutation changed the output interface"

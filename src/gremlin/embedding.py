from data_structures.case import Inapplicable
from .scope import analyze
from .render import render


def choose_site(query, pattern, rng):
    sites = []
    analyze(query, sites=sites)
    compatible = [site for site in sites if pattern.accepts(site)]
    return rng.choice(compatible) if compatible else None


def check_pair(source, mutant, case):
    assert analyze(source).current.kind == analyze(mutant).current.kind, "Mutation changed the output kind"
    if render(source) == render(mutant):
        raise Inapplicable("Mutation did not change the query")

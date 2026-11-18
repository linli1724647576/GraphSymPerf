def random_graph(graph, host_symbols, options, rng, support):
    # Generate typed background data without solving the host query's predicates.
    labels = [s for s in sorted(host_symbols) if s.kind == "label"] or ["Background"]
    types = [s for s in sorted(host_symbols) if s.kind == "relationship"] or ["BACKGROUND" if support["multi_label"] else "BACKGROUND_EDGE"]
    keys = [(s, s.kind[4:]) for s in sorted(host_symbols) if s.kind.startswith("key_")]
    for _ in range(options["background_nodes"]):
        chosen = rng.sample(labels, min(len(labels), 2 if support["multi_label"] and rng.random() < .2 else 1))
        props = {"keep": rng.random() < .7, "value": rng.randint(0, 5)}
        for key, kind in keys:
            if rng.random() < .8:
                props[key] = {"int": rng.randint(-2, 5), "float": rng.choice([0.5, 1.5, 2.5]),
                              "bool": rng.choice([True, False]), "string": rng.choice(["a", "b", "c"])}[kind]
        graph.node(chosen, **props)
    for _ in range(options["background_relationships"]):
        if graph.nodes:
            graph.edge(rng.randrange(len(graph.nodes)), rng.randrange(len(graph.nodes)), rng.choice(types),
                       keep=rng.random() < .7)

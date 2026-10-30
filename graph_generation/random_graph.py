def random_graph(graph, host_symbols, bindings, options, rng, support):
    # Generate typed background data without solving the host query's predicates.
    # 生成类型匹配的随机背景数据，不反向求解宿主查询的谓词。
    labels = [bindings[s.name] for s in sorted(host_symbols, key=lambda s: s.name) if s.kind == "label"] or ["Background"]
    types = [bindings[s.name] for s in sorted(host_symbols, key=lambda s: s.name) if s.kind == "relationship"] or ["BACKGROUND" if support["multi_label"] else "BACKGROUND_EDGE"]
    keys = [(bindings[s.name], s.kind[4:]) for s in sorted(host_symbols, key=lambda s: s.name) if s.kind.startswith("key_")]
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

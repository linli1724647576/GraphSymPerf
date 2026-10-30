def select_database(system):
    # Select only implemented backends; selection itself opens no connection.
    # 只选择已实现的后端；选择过程不建立数据库连接。
    if system == "neo4j":
        from . import neo4j
        return neo4j
    if system == "arcadedb":
        from . import arcadedb
        return arcadedb
    if system == "janusgraph":
        from . import janusgraph
        return janusgraph
    if system == "hugegraph":
        from . import hugegraph
        return hugegraph
    if system == "memgraph":
        from . import memgraph
        return memgraph
    if system == "ladybug":
        from . import ladybug
        return ladybug
    if system == "falkordb":
        from . import falkordb
        return falkordb
    raise ValueError(f"Database backend is not implemented: {system}")

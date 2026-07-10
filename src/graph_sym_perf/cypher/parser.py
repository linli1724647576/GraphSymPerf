"""Parser for a deliberately small, provable Cypher fragment."""

from __future__ import annotations

import re

from graph_sym_perf.cypher.ast import (
    AndPredicate,
    Clause,
    CypherQuery,
    EdgePattern,
    LiteralPredicate,
    MatchClause,
    NodePattern,
    PatternList,
    NullPredicate,
    PredicateTerm,
    Region,
    RelationshipPattern,
    VarLengthPathPattern,
)


class CypherParseError(ValueError):
    """Raised when Cypher is outside the provable AST fragment."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


IDENT_RE = r"[A-Za-z]\w*"
NODE_RE = rf"\(\s*{IDENT_RE}(?:\s*:\s*{IDENT_RE})*(?:\s*\{{[^{{}}]*\}})?\s*\)"
REL_RE = r"\[[^\]]*\]"
CLAUSE_RE = re.compile(
    r"\b(OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN|CALL|CREATE|MERGE|DELETE|SET|REMOVE)\b",
    re.I,
)
NODE_RE_COMPILED = re.compile(rf"^{NODE_RE}$", re.S)
NODE_TOKEN_RE = re.compile(NODE_RE, re.S)
REL_TOKEN_RE = re.compile(REL_RE, re.S)
EDGE_OUT_RE = re.compile(rf"^(?P<left>{NODE_RE})\s*-\s*(?P<rel>{REL_RE})\s*->\s*(?P<right>{NODE_RE})$", re.S)
EDGE_IN_RE = re.compile(rf"^(?P<left>{NODE_RE})\s*<-\s*(?P<rel>{REL_RE})\s*-\s*(?P<right>{NODE_RE})$", re.S)
EDGE_UNDIRECTED_RE = re.compile(
    rf"^(?P<left>{NODE_RE})\s*-\s*(?P<rel>{REL_RE})?\s*-\s*(?P<right>{NODE_RE})$",
    re.S,
)
PATH_UNDIRECTED_RE = re.compile(
    rf"^(?:(?P<path_var>{IDENT_RE})\s*=\s*)?"
    rf"(?P<left>{NODE_RE})\s*-\s*(?P<rel>\[[^\]]*\*[^\]]*\])\s*-\s*(?P<right>{NODE_RE})$",
    re.S,
)
NODE_VAR_RE = re.compile(r"\(\s*(?P<var>[A-Za-z]\w*)")
LABEL_RE = re.compile(r":\s*(?P<label>[A-Za-z]\w*)")
REL_VAR_RE = re.compile(r"\[\s*(?P<var>[A-Za-z]\w*)?")
REL_TYPE_RE = re.compile(r":\s*(?P<type>[A-Za-z]\w*)")
PROPERTY_MAP_RE = re.compile(r"\{(?P<body>[^{}]*)\}")
SAFE_OPERAND_RE = re.compile(
    rf"^(?:{IDENT_RE}(?:\.{IDENT_RE})?|\${IDENT_RE}|-?\d+(?:\.\d+)?|"
    r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|true|false|null)$",
    re.I,
)
FORBIDDEN_PREDICATE_RE = re.compile(
    r"\b(rand|randomuuid|timestamp|datetime|localdatetime|date|time|duration|apoc\.|"
    r"exists|case|when|then|else|end|all|any|none|single)\b",
    re.I,
)

_ALPHA_KEYWORDS = frozenset(
    {
        "AS",
        "AND",
        "BY",
        "CALL",
        "CREATE",
        "DELETE",
        "FALSE",
        "LIMIT",
        "MATCH",
        "MERGE",
        "NULL",
        "OPTIONAL",
        "OR",
        "ORDER",
        "REMOVE",
        "RETURN",
        "SET",
        "SKIP",
        "TRUE",
        "UNWIND",
        "WHERE",
        "WITH",
    }
)


def alpha_normalize_query(query: str) -> str:
    """Normalize declared Cypher variables without touching labels or literals."""

    tokens = _identifier_spans(query)
    variables: dict[str, str] = {}
    for index, (start, end, name) in enumerate(tokens):
        previous_char = _previous_nonspace(query, start)
        next_char = _next_nonspace(query, end)
        previous_token = tokens[index - 1][2].upper() if index else ""
        is_path_variable = next_char == "=" and _next_nonspace(query, _next_nonspace_index(query, end) + 1) == "("
        is_node_or_relationship_variable = previous_char in {"(", "["}
        is_alias = previous_token == "AS"
        if (
            name.upper() not in _ALPHA_KEYWORDS
            and (is_path_variable or is_node_or_relationship_variable or is_alias)
        ):
            variables.setdefault(name, f"v{len(variables)}")

    pieces: list[str] = []
    cursor = 0
    for start, end, name in tokens:
        pieces.append(query[cursor:start])
        previous_char = _previous_nonspace(query, start)
        next_char = _next_nonspace(query, end)
        is_map_key = previous_char in {"{", ","} and next_char == ":"
        is_non_variable_role = previous_char in {":", ".", "$"} or is_map_key
        pieces.append(variables.get(name, name) if not is_non_variable_role else name)
        cursor = end
    pieces.append(query[cursor:])
    return " ".join("".join(pieces).upper().split())


def _identifier_spans(query: str) -> tuple[tuple[int, int, str], ...]:
    spans: list[tuple[int, int, str]] = []
    index = 0
    quote: str | None = None
    while index < len(query):
        char = query[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(query) and (query[end].isalnum() or query[end] == "_"):
                end += 1
            spans.append((index, end, query[index:end]))
            index = end
            continue
        index += 1
    return tuple(spans)


def _previous_nonspace(query: str, index: int) -> str:
    index -= 1
    while index >= 0 and query[index].isspace():
        index -= 1
    return query[index] if index >= 0 else ""


def _next_nonspace_index(query: str, index: int) -> int:
    while index < len(query) and query[index].isspace():
        index += 1
    return index


def _next_nonspace(query: str, index: int) -> str:
    index = _next_nonspace_index(query, index)
    return query[index] if index < len(query) else ""


def parse_query(query: str) -> CypherQuery:
    return CypherQuery(text=query, clauses=_clauses(query))


def parse_match_clause(clause: Clause) -> MatchClause:
    if clause.keyword != "MATCH":
        raise CypherParseError("NOT_MATCH_CLAUSE", "expected an ordinary MATCH clause")
    keyword = re.match(r"\s*MATCH\b", clause.text, re.I)
    if keyword is None:
        raise CypherParseError("NOT_MATCH_CLAUSE", "expected an ordinary MATCH clause")
    body_start = keyword.end()
    where_start = _top_level_keyword_start(clause.text, "WHERE", body_start)
    pattern_end = where_start if where_start is not None else len(clause.text)
    raw_pattern = clause.text[body_start:pattern_end]
    pattern_text = raw_pattern.strip()
    if not pattern_text:
        raise CypherParseError("EMPTY_MATCH_PATTERN", "MATCH clause has no pattern body")
    leading = len(raw_pattern) - len(raw_pattern.lstrip())
    trailing = len(raw_pattern) - len(raw_pattern.rstrip())
    pattern_region = Region(start=body_start + leading, end=pattern_end - trailing, text=pattern_text)
    where_region = None
    if where_start is not None:
        where_body_start = where_start + len("WHERE")
        where_text = clause.text[where_body_start:].strip()
        leading_where = len(clause.text[where_body_start:]) - len(clause.text[where_body_start:].lstrip())
        where_region = Region(start=where_body_start + leading_where, end=len(clause.text), text=where_text)
    return MatchClause(
        clause=clause,
        pattern_region=pattern_region,
        pattern=parse_pattern_atom(pattern_text),
        where_region=where_region,
    )


def parse_pattern_atom(pattern: str) -> NodePattern | EdgePattern | VarLengthPathPattern | PatternList:
    stripped = pattern.strip()
    parts = split_top_level_commas(stripped)
    if len(parts) > 1:
        return PatternList(text=stripped, atoms=tuple(parse_pattern_atom(part) for part in parts))
    if NODE_RE_COMPILED.fullmatch(stripped):
        return _parse_node(stripped)
    path = PATH_UNDIRECTED_RE.fullmatch(stripped)
    if path is not None:
        rel = _parse_relationship(path.group("rel"))
        return VarLengthPathPattern(
            text=stripped,
            left=_parse_node(path.group("left")),
            relationship=rel,
            right=_parse_node(path.group("right")),
            direction="undirected",
            path_variable=path.group("path_var"),
        )
    out = EDGE_OUT_RE.fullmatch(stripped)
    if out is not None:
        return EdgePattern(
            text=stripped,
            left=_parse_node(out.group("left")),
            relationship=_parse_relationship(out.group("rel")),
            right=_parse_node(out.group("right")),
            direction="out",
        )
    incoming = EDGE_IN_RE.fullmatch(stripped)
    if incoming is not None:
        return EdgePattern(
            text=stripped,
            left=_parse_node(incoming.group("left")),
            relationship=_parse_relationship(incoming.group("rel")),
            right=_parse_node(incoming.group("right")),
            direction="in",
        )
    undirected = EDGE_UNDIRECTED_RE.fullmatch(stripped)
    if undirected is not None:
        return EdgePattern(
            text=stripped,
            left=_parse_node(undirected.group("left")),
            relationship=_parse_relationship(undirected.group("rel") or "[]"),
            right=_parse_node(undirected.group("right")),
            direction="undirected",
        )
    chain = _parse_chain_pattern(stripped)
    if chain is not None:
        return chain
    raise CypherParseError("UNPROVEN_PATTERN_ATOM", "pattern is outside the AST proof fragment")


def parse_and_predicate(predicate: str, *, allow_ordering: bool = False, allow_in: bool = False) -> AndPredicate:
    stripped = _strip_balanced_outer_parens(predicate.strip())
    if FORBIDDEN_PREDICATE_RE.search(stripped):
        raise CypherParseError("UNSAFE_PREDICATE", "predicate contains forbidden constructs")
    parts = _flatten_and_terms(stripped)
    if len(parts) < 2:
        raise CypherParseError("NO_TOP_LEVEL_AND", "expected at least two top-level AND terms")
    terms = tuple(_parse_predicate_term(part, allow_ordering=allow_ordering, allow_in=allow_in) for part in parts)
    return AndPredicate(text=stripped, terms=terms)


def _parse_node(raw: str) -> NodePattern:
    match = NODE_VAR_RE.search(raw)
    if match is None:
        raise CypherParseError("UNNAMED_NODE", "proof fragment requires named node variables")
    return NodePattern(
        text=raw,
        variable=match.group("var"),
        labels=tuple(LABEL_RE.findall(raw)),
        properties=_parse_property_map(raw),
    )


def _parse_relationship(raw: str) -> RelationshipPattern:
    variable_match = REL_VAR_RE.search(raw)
    variable = None
    if variable_match is not None:
        candidate = variable_match.group("var")
        if candidate:
            after = raw[variable_match.end("var") :].lstrip()
            if after.startswith(":") or after.startswith("*") or after.startswith("]"):
                variable = candidate
    bounds = _var_length_bounds(raw)
    prefix = _var_length_prefix(raw)
    return RelationshipPattern(
        text=raw,
        variable=variable,
        types=tuple(REL_TYPE_RE.findall(prefix if prefix is not None else raw)),
        var_length=bounds,
        exposes_relationship_list=bool(bounds and prefix and not prefix.strip().startswith(":")),
        properties=_parse_property_map(raw),
    )


def _parse_chain_pattern(raw: str) -> PatternList | None:
    index = 0
    first_node = NODE_TOKEN_RE.match(raw, index)
    if first_node is None:
        return None
    current = _parse_node(first_node.group(0))
    index = first_node.end()
    atoms: list[EdgePattern] = []
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if raw.startswith("<-", index):
            left_connector = "<-"
            index += 2
        elif raw.startswith("-", index):
            left_connector = "-"
            index += 1
        else:
            return None
        while index < len(raw) and raw[index].isspace():
            index += 1
        rel_match = REL_TOKEN_RE.match(raw, index)
        if rel_match is None:
            return None
        rel_text = rel_match.group(0)
        if "*" in rel_text:
            return None
        relationship = _parse_relationship(rel_text)
        index = rel_match.end()
        while index < len(raw) and raw[index].isspace():
            index += 1
        if raw.startswith("->", index):
            right_connector = "->"
            index += 2
        elif raw.startswith("-", index):
            right_connector = "-"
            index += 1
        else:
            return None
        while index < len(raw) and raw[index].isspace():
            index += 1
        next_node = NODE_TOKEN_RE.match(raw, index)
        if next_node is None:
            return None
        right = _parse_node(next_node.group(0))
        index = next_node.end()
        if left_connector == "-" and right_connector == "->":
            direction = "out"
        elif left_connector == "<-" and right_connector == "-":
            direction = "in"
        elif left_connector == "-" and right_connector == "-":
            direction = "undirected"
        else:
            return None
        atoms.append(
            EdgePattern(
                text=f"{current.render()}{left_connector}{relationship.render()}{right_connector}{right.render()}",
                left=current,
                relationship=relationship,
                right=right,
                direction=direction,
            )
        )
        current = right
    if len(atoms) < 2:
        return None
    return PatternList(text=raw, atoms=tuple(atoms), separator="chain")


def _parse_predicate_term(raw: str, *, allow_ordering: bool = False, allow_in: bool = False) -> PredicateTerm:
    term = _strip_balanced_outer_parens(raw.strip())
    if not term:
        raise CypherParseError("EMPTY_PREDICATE_TERM", "empty predicate term")
    if FORBIDDEN_PREDICATE_RE.search(term) or re.search(r"\b(OR|XOR)\b", term, re.I):
        raise CypherParseError("UNSAFE_PREDICATE_TERM", "predicate term is not pure conjunction")
    forbidden_symbols = ("{", "}", "/", "%", "+") if allow_in else ("[", "]", "{", "}", "/", "%", "+")
    if any(symbol in term for symbol in forbidden_symbols):
        raise CypherParseError("UNSAFE_PREDICATE_TERM", "predicate term may be exception-sensitive")
    if term.lower() in {"true", "false", "null"}:
        return LiteralPredicate(text=term, operator="literal", operands=(term.lower(),))
    null_match = re.match(r"^(?P<operand>.+?)\s+IS\s+(?P<not>NOT\s+)?NULL$", term, re.I)
    if null_match:
        operand = _safe_operand(null_match.group("operand"))
        operator = "IS NOT NULL" if null_match.group("not") else "IS NULL"
        return NullPredicate(text=term, operator=operator, operands=(operand,))
    if allow_in:
        in_match = re.match(r"^(?P<operand>.+?)\s+IN\s+\[(?P<values>.*)\]$", term, re.I | re.S)
        if in_match:
            operand = _safe_operand(in_match.group("operand"))
            values = tuple(_safe_operand(item) for item in split_top_level_commas(in_match.group("values")))
            return PredicateTerm(text=term, operator="IN", operands=(operand,) + values)
    comparison = _split_top_level_comparison(term)
    if comparison is None:
        raise CypherParseError("UNPROVEN_PREDICATE_TERM", "predicate term is outside total comparison grammar")
    left, operator, right = comparison
    if operator not in {"=", "<>"} and not allow_ordering:
        raise CypherParseError("UNPROVEN_PREDICATE_TERM", "ordering comparison is outside total proof grammar")
    return PredicateTerm(text=term, operator=operator, operands=(_safe_operand(left), _safe_operand(right)))


def _safe_operand(raw: str) -> str:
    operand = _strip_balanced_outer_parens(raw.strip())
    if not operand or re.search(r"\b[A-Za-z]\w*\s*\(", operand):
        raise CypherParseError("UNSAFE_OPERAND", "operand is outside the pure total grammar")
    if SAFE_OPERAND_RE.fullmatch(operand) is None:
        raise CypherParseError("UNSAFE_OPERAND", "operand is outside the pure total grammar")
    return operand


def _clauses(query: str) -> tuple[Clause, ...]:
    matches = [
        match
        for match in CLAUSE_RE.finditer(query)
        if not _inside_string(query, match.start()) and _is_clause_keyword(query, match)
    ]
    clauses: list[Clause] = []
    for index, match in enumerate(matches):
        keyword = " ".join(match.group(1).upper().split())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(query)
        clauses.append(Clause(keyword=keyword, start=match.start(), end=end, text=query[match.start() : end]))
    return tuple(clauses)


def _flatten_and_terms(predicate: str) -> tuple[str, ...]:
    stripped = _strip_balanced_outer_parens(predicate.strip())
    parts = _split_top_level_and(stripped)
    if len(parts) <= 1:
        return (stripped,)
    flattened: list[str] = []
    for part in parts:
        flattened.extend(_flatten_and_terms(part))
    return tuple(item for item in flattened if item)


def _split_top_level_and(predicate: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(predicate):
        char = predicate[index]
        if quote:
            if char == quote and not _escaped(predicate, index):
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(depth - 1, 0)
            index += 1
            continue
        if depth == 0 and predicate[index : index + 3].upper() == "AND":
            before = predicate[index - 1] if index > 0 else " "
            after = predicate[index + 3] if index + 3 < len(predicate) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                parts.append(predicate[start:index].strip())
                start = index + 3
                index += 3
                continue
        index += 1
    parts.append(predicate[start:].strip())
    return tuple(part for part in parts if part)


def _split_top_level_comparison(term: str) -> tuple[str, str, str] | None:
    operators = ("<=", ">=", "<>", "=", "<", ">")
    quote: str | None = None
    depth = 0
    index = 0
    while index < len(term):
        char = term[index]
        if quote:
            if char == quote and not _escaped(term, index):
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(depth - 1, 0)
            index += 1
            continue
        if depth == 0:
            for operator in operators:
                if term.startswith(operator, index):
                    return term[:index].strip(), operator, term[index + len(operator) :].strip()
        index += 1
    return None


def _top_level_keyword_start(text: str, keyword: str, start: int) -> int | None:
    quote: str | None = None
    depth = 0
    upper_keyword = keyword.upper()
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote and not _escaped(text, index):
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(depth - 1, 0)
            index += 1
            continue
        if depth == 0 and text[index : index + len(keyword)].upper() == upper_keyword:
            before = text[index - 1] if index > 0 else " "
            after_index = index + len(keyword)
            after = text[after_index] if after_index < len(text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return index
        index += 1
    return None


def split_top_level_commas(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote and not _escaped(text, index):
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}":
            depth = max(depth - 1, 0)
            continue
        if char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return tuple(part for part in parts if part)


def _parse_property_map(raw: str) -> tuple[tuple[str, str], ...]:
    match = PROPERTY_MAP_RE.search(raw)
    if match is None:
        return ()
    entries: list[tuple[str, str]] = []
    for item in split_top_level_commas(match.group("body")):
        if ":" not in item:
            raise CypherParseError("UNPROVEN_PROPERTY_MAP", "property map entry is outside key-value grammar")
        key, value = item.split(":", 1)
        key = key.strip()
        value = value.strip()
        if re.fullmatch(IDENT_RE, key) is None:
            raise CypherParseError("UNPROVEN_PROPERTY_MAP", "property key is not a static identifier")
        _safe_operand(value)
        entries.append((key, value))
    keys = [key for key, _ in entries]
    if len(keys) != len(set(keys)):
        raise CypherParseError("DUPLICATE_PROPERTY_KEY", "property map keys must be unique")
    return tuple(entries)


def _var_length_bounds(rel: str) -> tuple[int, int] | None:
    match = re.search(r"\*\s*(?P<lower>\d+)\s*\.\.\s*(?P<upper>\d+)", rel)
    if match is None:
        return None
    lower = int(match.group("lower"))
    upper = int(match.group("upper"))
    if lower > upper:
        raise CypherParseError("INVALID_PATH_BOUNDS", "path lower bound exceeds upper bound")
    return lower, upper


def _var_length_prefix(rel: str) -> str | None:
    body = rel.strip()[1:-1].strip()
    star = body.find("*")
    return body[:star].strip() if star >= 0 else None


def _strip_balanced_outer_parens(text: str) -> str:
    stripped = text.strip()
    while stripped.startswith("(") and stripped.endswith(")") and _outer_parens_wrap(stripped):
        stripped = stripped[1:-1].strip()
    return stripped


def _outer_parens_wrap(text: str) -> bool:
    quote: str | None = None
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote and not _escaped(text, index):
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return False
    return depth == 0 and quote is None


def _is_clause_keyword(query: str, match: re.Match[str]) -> bool:
    keyword = " ".join(match.group(1).upper().split())
    if keyword != "WITH":
        return True
    prefix = query[: match.start()].rstrip().upper()
    return not (prefix.endswith("STARTS") or prefix.endswith("ENDS"))


def _inside_string(text: str, position: int) -> bool:
    quote: str | None = None
    escaped = False
    for char in text[:position]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is None and char in {"'", '"'}:
            quote = char
            continue
        if quote == char:
            quote = None
    return quote is not None


def _escaped(text: str, position: int) -> bool:
    count = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1

"""Conservative paired AST deletion proposals for the provable Cypher fragment."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from graph_sym_perf.cypher.ast import PatternList
from graph_sym_perf.cypher.parser import (
    CypherParseError,
    parse_and_predicate,
    parse_match_clause,
    parse_query,
    split_top_level_commas,
)
from graph_sym_perf.reducer.dependencies import analyze_query, dependency_preserved, dependency_safe_removal
from graph_sym_perf.runtime.models import EquivalentQueryPair


@dataclass(frozen=True)
class ReductionProposal:
    name: str
    query: str
    layer: str = "clause"


@dataclass(frozen=True)
class PairedReductionProposal:
    canonical_id: str
    candidate: EquivalentQueryPair
    layer: str


@dataclass(frozen=True)
class _SingleEdit:
    edit_key: str
    layer: str
    query: str


@dataclass(frozen=True)
class GraphSetupProposalResult:
    proposals: tuple[str, ...]
    reason: str


_LEVELS = ("clause", "pattern_atom", "projection_item", "predicate_subtree", "literal_item", "list_item")
_LITERAL_VALUE = re.compile(
    r"(?:-?\d+(?:\.\d+)?|true|false|null|\$[A-Za-z_]\w*|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")",
    re.I,
)


def split_clauses(query: str) -> tuple[str, ...]:
    return tuple(clause.text.strip() for clause in parse_query(query).clauses)


def clause_removal_proposals(query: str) -> tuple[ReductionProposal, ...]:
    return tuple(
        ReductionProposal(edit.edit_key, edit.query, edit.layer)
        for edit in _clause_edits(query)
    )


def paired_reduction_proposals(pair: EquivalentQueryPair) -> tuple[PairedReductionProposal, ...]:
    """Enumerate the public one-step universe; every edit changes both query sides."""

    graph_a = analyze_query(pair.query_a)
    graph_b = analyze_query(pair.query_b)
    if (
        not graph_a.valid
        or not graph_b.valid
        or graph_a.aggregate_sensitive
        or graph_b.aggregate_sensitive
        or graph_a.output_schema != graph_b.output_schema
    ):
        return ()
    edits_a = _all_edits(pair.query_a)
    edits_b = _all_edits(pair.query_b)
    by_key_b = {(edit.layer, edit.edit_key): edit for edit in edits_b}
    proposals: list[PairedReductionProposal] = []
    seen_pairs: set[tuple[str, str]] = set()
    for edit_a in edits_a:
        edit_b = by_key_b.get((edit_a.layer, edit_a.edit_key))
        if edit_b is None:
            continue
        candidate = EquivalentQueryPair(
            edit_a.query,
            edit_b.query,
            pair.parameters,
            pair.graph_snapshot_hash,
            pair.dbms_fingerprint,
        )
        pair_key = (candidate.query_a, candidate.query_b)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        proposals.append(
            PairedReductionProposal(
                canonical_id=f"{edit_a.layer}:{edit_a.edit_key}",
                candidate=candidate,
                layer=edit_a.layer,
            )
        )
    order = {name: index for index, name in enumerate(_LEVELS)}
    return tuple(sorted(proposals, key=lambda item: (order[item.layer], item.canonical_id)))


def hierarchical_paired_proposals(pair: EquivalentQueryPair) -> tuple[PairedReductionProposal, ...]:
    """Try two-deletion chunks before refining to the public one-step universe."""

    one_step = paired_reduction_proposals(pair)
    chunks: list[PairedReductionProposal] = []
    seen = {(item.candidate.query_a, item.candidate.query_b) for item in one_step}
    for first in one_step:
        for second in paired_reduction_proposals(first.candidate):
            if second.layer != first.layer:
                continue
            key = (second.candidate.query_a, second.candidate.query_b)
            if key in seen:
                continue
            seen.add(key)
            chunks.append(
                PairedReductionProposal(
                    canonical_id=f"chunk:{first.canonical_id}+{second.canonical_id}",
                    candidate=second.candidate,
                    layer=f"{first.layer}_chunk",
                )
            )
            if len(chunks) >= 64:
                break
        if len(chunks) >= 64:
            break
    return tuple(chunks) + one_step


def graph_setup_proposals(_statements: tuple[str, ...]) -> GraphSetupProposalResult:
    """Fail closed until setup-chunk deletion has a graph-semantic equivalence proof."""

    return GraphSetupProposalResult((), "graph_setup_semantic_proof_unavailable")


def _all_edits(query: str) -> tuple[_SingleEdit, ...]:
    return (
        *_clause_edits(query),
        *_pattern_edits(query),
        *_projection_edits(query),
        *_predicate_edits(query),
        *_literal_item_edits(query),
        *_list_item_edits(query),
    )


def _clause_edits(query: str) -> tuple[_SingleEdit, ...]:
    parsed = parse_query(query)
    clauses = tuple(clause.text.strip() for clause in parsed.clauses)
    edits: list[_SingleEdit] = []
    occurrences: dict[str, int] = {}
    for index, clause in enumerate(parsed.clauses):
        if clause.keyword == "RETURN" or not dependency_safe_removal(clauses, index):
            continue
        candidate = _replace_query_region(query, clause.start, clause.end, "")
        if dependency_preserved(query, candidate):
            signature = _canonical_text(clause.text)
            occurrence = occurrences.get(signature, 0)
            occurrences[signature] = occurrence + 1
            edits.append(_SingleEdit(_semantic_key("clause", signature, occurrence), "clause", candidate))
    return tuple(edits)


def _pattern_edits(query: str) -> tuple[_SingleEdit, ...]:
    parsed = parse_query(query)
    edits: list[_SingleEdit] = []
    match_ordinal = 0
    for clause in parsed.clauses:
        if clause.keyword != "MATCH":
            continue
        try:
            match = parse_match_clause(clause)
        except CypherParseError:
            match_ordinal += 1
            continue
        pattern = match.pattern
        if not isinstance(pattern, PatternList) or len(pattern.atoms) < 2:
            match_ordinal += 1
            continue
        occurrences: dict[str, int] = {}
        for atom_index in range(len(pattern.atoms)):
            remaining = pattern.atoms[:atom_index] + pattern.atoms[atom_index + 1 :]
            if pattern.separator == "chain":
                replacement = PatternList(pattern.text, remaining, "chain").render()
            else:
                replacement = ", ".join(atom.render() for atom in remaining)
            clause_text = match.replace_pattern(replacement)
            candidate = _replace_query_region(query, clause.start, clause.end, clause_text)
            if dependency_preserved(query, candidate):
                signature = _canonical_text(pattern.atoms[atom_index].render())
                occurrence = occurrences.get(signature, 0)
                occurrences[signature] = occurrence + 1
                edits.append(
                    _SingleEdit(
                        _semantic_key(f"match_{match_ordinal}_atom", signature, occurrence),
                        "pattern_atom",
                        candidate,
                    )
                )
        match_ordinal += 1
    return tuple(edits)


def _projection_edits(query: str) -> tuple[_SingleEdit, ...]:
    parsed = parse_query(query)
    edits: list[_SingleEdit] = []
    with_ordinal = 0
    for clause in parsed.clauses:
        if clause.keyword != "WITH":
            continue
        body = clause.text[len("WITH") :].strip()
        projection, suffix = _split_projection_suffix(body)
        items = split_top_level_commas(projection)
        if len(items) < 2:
            with_ordinal += 1
            continue
        occurrences: dict[str, int] = {}
        for item_index in range(len(items)):
            replacement = "WITH " + ", ".join(items[:item_index] + items[item_index + 1 :])
            if suffix:
                replacement += " " + suffix
            candidate = _replace_query_region(query, clause.start, clause.end, replacement)
            if dependency_preserved(query, candidate):
                signature = _canonical_text(items[item_index])
                occurrence = occurrences.get(signature, 0)
                occurrences[signature] = occurrence + 1
                edits.append(
                    _SingleEdit(
                        _semantic_key(f"with_{with_ordinal}_item", signature, occurrence),
                        "projection_item",
                        candidate,
                    )
                )
        with_ordinal += 1
    return tuple(edits)


def _predicate_edits(query: str) -> tuple[_SingleEdit, ...]:
    parsed = parse_query(query)
    edits: list[_SingleEdit] = []
    where_ordinal = 0
    for clause in parsed.clauses:
        if clause.keyword != "MATCH":
            continue
        try:
            match = parse_match_clause(clause)
            if match.where_region is None:
                continue
            terms = _predicate_terms(match.where_region.text)
        except CypherParseError:
            continue
        occurrences: dict[str, int] = {}
        for term_index in range(len(terms)):
            remaining = terms[:term_index] + terms[term_index + 1 :]
            if remaining:
                replacement = " AND ".join(term.text for term in remaining)
                clause_text = (
                    clause.text[: match.where_region.start]
                    + replacement
                    + clause.text[match.where_region.end :]
                )
            else:
                where_start = _where_keyword_start(clause.text, match.pattern_region.end, match.where_region.start)
                if where_start is None:
                    continue
                clause_text = (
                    clause.text[:where_start].rstrip()
                    + clause.text[match.where_region.end :]
                )
            candidate = _replace_query_region(query, clause.start, clause.end, clause_text)
            if dependency_preserved(query, candidate):
                signature = _canonical_text(terms[term_index].text)
                occurrence = occurrences.get(signature, 0)
                occurrences[signature] = occurrence + 1
                edits.append(
                    _SingleEdit(
                        _semantic_key(f"where_{where_ordinal}_term", signature, occurrence),
                        "predicate_subtree",
                        candidate,
                    )
                )
        where_ordinal += 1
    return tuple(edits)


def _list_item_edits(query: str) -> tuple[_SingleEdit, ...]:
    edits: list[_SingleEdit] = []
    parsed_query = parse_query(query)
    list_index = 0
    for clause in parsed_query.clauses:
        if clause.keyword != "MATCH":
            continue
        try:
            match_clause = parse_match_clause(clause)
            if match_clause.where_region is None:
                continue
            _predicate_terms(match_clause.where_region.text)
        except CypherParseError:
            continue
        where_text = match_clause.where_region.text
        for start, end in _delimited_regions(where_text, "[", "]"):
            if _previous_word(where_text, start) != "IN":
                continue
            body = where_text[start + 1 : end - 1]
            items = split_top_level_commas(body)
            if not items:
                continue
            body_start = clause.start + match_clause.where_region.start + start + 1
            body_end = clause.start + match_clause.where_region.start + end - 1
            occurrences: dict[str, int] = {}
            list_signature = _canonical_text(body)
            for item_index in range(len(items)):
                replacement = ", ".join(items[:item_index] + items[item_index + 1 :])
                candidate = query[:body_start] + replacement + query[body_end:]
                if dependency_preserved(query, candidate):
                    item_signature = _canonical_text(items[item_index])
                    occurrence = occurrences.get(item_signature, 0)
                    occurrences[item_signature] = occurrence + 1
                    edits.append(
                        _SingleEdit(
                            _semantic_key(
                                f"list_{list_index}_item",
                                f"{list_signature}\0{item_signature}",
                                occurrence,
                            ),
                            "list_item",
                            candidate,
                        )
                    )
            list_index += 1
    return tuple(edits)


def _literal_item_edits(query: str) -> tuple[_SingleEdit, ...]:
    edits: list[_SingleEdit] = []
    parsed_query = parse_query(query)
    map_index = 0
    for clause in parsed_query.clauses:
        if clause.keyword != "MATCH":
            continue
        try:
            match_clause = parse_match_clause(clause)
        except CypherParseError:
            continue
        pattern_text = match_clause.pattern_region.text
        for start, end in _delimited_regions(pattern_text, "{", "}"):
            body = pattern_text[start + 1 : end - 1]
            items = split_top_level_commas(body)
            parsed: list[tuple[str, str]] = []
            for item in items:
                entry = re.fullmatch(
                    rf"(?P<key>[A-Za-z_]\w*)\s*:\s*(?P<value>{_LITERAL_VALUE.pattern})",
                    item.strip(),
                    re.I | re.S,
                )
                if entry is None:
                    parsed = []
                    break
                parsed.append((entry.group("key"), entry.group("value")))
            if not parsed:
                continue
            region_start = clause.start + match_clause.pattern_region.start + start
            region_end = clause.start + match_clause.pattern_region.start + end
            occurrences: dict[str, int] = {}
            map_signature = _canonical_text(body)
            for item_index, (key, value) in enumerate(parsed):
                remaining = parsed[:item_index] + parsed[item_index + 1 :]
                replacement = "{" + ", ".join(f"{name}: {literal}" for name, literal in remaining) + "}"
                candidate = query[:region_start] + replacement + query[region_end:]
                if dependency_preserved(query, candidate):
                    signature = f"{key}:{value}"
                    occurrence = occurrences.get(signature, 0)
                    occurrences[signature] = occurrence + 1
                    edits.append(
                        _SingleEdit(
                            _semantic_key(
                                f"map_{map_index}_literal",
                                f"{map_signature}\0{signature}",
                                occurrence,
                            ),
                            "literal_item",
                            candidate,
                        )
                    )
            map_index += 1
    return tuple(edits)


def _predicate_terms(text: str):
    try:
        return parse_and_predicate(text, allow_ordering=True, allow_in=True).terms
    except CypherParseError as exc:
        if exc.code != "NO_TOP_LEVEL_AND":
            raise
    probe = parse_and_predicate(f"({text}) AND true", allow_ordering=True, allow_in=True)
    return probe.terms[:1]


def _where_keyword_start(text: str, start: int, end: int) -> int | None:
    region = _mask_lexical_noncode(text[start:end])
    matches = tuple(re.finditer(r"\bWHERE\b", region, re.I))
    return start + matches[-1].start() if matches else None


def _previous_word(text: str, index: int) -> str:
    prefix = text[:index].rstrip()
    match = re.search(r"([A-Za-z_]\w*)$", prefix)
    return match.group(1).upper() if match else ""


def _delimited_regions(text: str, opener: str, closer: str) -> tuple[tuple[int, int], ...]:
    masked = _mask_lexical_noncode(text)
    stack: list[int] = []
    regions: list[tuple[int, int]] = []
    for index, char in enumerate(masked):
        if char == opener:
            stack.append(index)
        elif char == closer and stack:
            start = stack.pop()
            if not stack:
                regions.append((start, index + 1))
    return tuple(regions)


def _mask_lexical_noncode(text: str) -> str:
    result = list(text)
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            result[index] = " "
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            result[index] = " "
            if char == "*" and next_char == "/":
                result[index + 1] = " "
                block_comment = False
                index += 1
        elif quote is not None:
            result[index] = " "
            if char == "\\" and next_char:
                result[index + 1] = " "
                index += 1
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            result[index] = " "
        elif char == "/" and next_char == "/":
            result[index] = result[index + 1] = " "
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            result[index] = result[index + 1] = " "
            block_comment = True
            index += 1
        index += 1
    return "".join(result)


def _split_projection_suffix(body: str) -> tuple[str, str]:
    masked = _mask_nested(body)
    starts = [
        match.start()
        for match in re.finditer(r"\b(?:WHERE|ORDER\s+BY|SKIP|LIMIT)\b", masked, re.I)
    ]
    if not starts:
        return body.strip(), ""
    start = min(starts)
    return body[:start].strip(), body[start:].strip()


def _mask_nested(text: str) -> str:
    result = list(text)
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            result[index] = " "
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            result[index] = " "
        elif char in "([{":
            depth += 1
            result[index] = " "
        elif char in ")]}":
            result[index] = " "
            depth = max(0, depth - 1)
        elif depth:
            result[index] = " "
    return "".join(result)


def _replace_query_region(query: str, start: int, end: int, replacement: str) -> str:
    before = query[:start].rstrip()
    after = query[end:].lstrip()
    pieces = tuple(piece for piece in (before, replacement.strip(), after) if piece)
    return " ".join(pieces)


def _canonical_text(text: str) -> str:
    return " ".join(text.split())


def _semantic_key(prefix: str, text: str, occurrence: int) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}_{occurrence}"

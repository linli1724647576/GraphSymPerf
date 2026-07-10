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
            predicate = parse_and_predicate(match.where_region.text, allow_ordering=True, allow_in=True)
        except CypherParseError:
            continue
        occurrences: dict[str, int] = {}
        for term_index in range(len(predicate.terms)):
            remaining = predicate.terms[:term_index] + predicate.terms[term_index + 1 :]
            replacement = " AND ".join(term.text for term in remaining)
            clause_text = (
                clause.text[: match.where_region.start]
                + replacement
                + clause.text[match.where_region.end :]
            )
            candidate = _replace_query_region(query, clause.start, clause.end, clause_text)
            if dependency_preserved(query, candidate):
                signature = _canonical_text(predicate.terms[term_index].text)
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
    for list_index, match in enumerate(re.finditer(r"\bIN\s*\[(?P<body>[^\[\]]+)\]", query, re.I | re.S)):
        items = split_top_level_commas(match.group("body"))
        if len(items) < 2:
            continue
        body_start = match.start("body")
        body_end = match.end("body")
        occurrences: dict[str, int] = {}
        list_signature = _canonical_text(match.group("body"))
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
    return tuple(edits)


def _literal_item_edits(query: str) -> tuple[_SingleEdit, ...]:
    edits: list[_SingleEdit] = []
    for map_index, match in enumerate(re.finditer(r"\{(?P<body>[^{}]+)\}", query, re.S)):
        items = split_top_level_commas(match.group("body"))
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
        occurrences: dict[str, int] = {}
        map_signature = _canonical_text(match.group("body"))
        for item_index, (key, value) in enumerate(parsed):
            remaining = parsed[:item_index] + parsed[item_index + 1 :]
            replacement = "{" + ", ".join(f"{name}: {literal}" for name, literal in remaining) + "}"
            candidate = query[: match.start()] + replacement + query[match.end() :]
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
    return tuple(edits)


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

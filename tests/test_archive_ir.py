"""Archive integrity across legacy identities and shared-IR execution metadata."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import main
from generator import load_seeds
from query import QueryError


def case(target="neo4j", pattern="P1"):
    fixture = Path(__file__).parent / "fixtures/gdsmith/neo4j_seeds.log"
    source = load_seeds({"log_files": [str(fixture)]})[0]
    return main.prepare_legacy_case(source, pattern, 2, 0, 0, database={"dbms": target})


def change_record(directory, update):
    path = directory / "result.json"
    record = json.loads(path.read_text())
    update(record)
    path.write_text(json.dumps(record))


@pytest.mark.parametrize("version", [1, 2])
def test_extended_legacy_archive_preserves_input_hash_and_roundtrips(tmp_path, version):
    original = case()
    legacy = {key: value for key, value in original.items() if key not in main.CASE_EXTENSION_FIELDS}
    identity = main.case_digest(legacy, version)
    assert main.case_digest(original, version) == identity
    main.save_case(tmp_path, original, {}, {"status": "no_candidate"}, format_version=version)
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["input_hash"] == identity
    assert record["extension_hash"] == main.extension_digest(original)
    assert main.read_case(tmp_path)[0] == original


@pytest.mark.parametrize("version", [1, 2])
def test_true_legacy_archive_needs_no_extension_hash(tmp_path, version):
    original = {key: value for key, value in case().items() if key not in main.CASE_EXTENSION_FIELDS}
    main.save_case(tmp_path, original, {}, {}, format_version=version)
    assert "extension_hash" not in json.loads((tmp_path / "result.json").read_text())
    assert main.read_case(tmp_path)[0] == original


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field", main.CASE_EXTENSION_FIELDS)
def test_legacy_extension_changes_cannot_hide_behind_original_hash(tmp_path, version, field):
    original = case()
    original.setdefault(field, None)
    main.save_case(tmp_path, original, {}, {}, format_version=version)
    def update(record):
        record["case"][field] = {"changed": True}
    change_record(tmp_path, update)
    with pytest.raises(ValueError, match="metadata have changed"):
        main.read_case(tmp_path)


@pytest.mark.parametrize("operation", ["remove_hash", "delete_extensions"])
def test_legacy_metadata_requires_the_full_extension_digest(tmp_path, operation):
    main.save_case(tmp_path, case(), {}, {}, format_version=2)
    def update(record):
        if operation == "remove_hash":
            record.pop("extension_hash")
        else:
            for field in main.CASE_EXTENSION_FIELDS:
                record["case"].pop(field, None)
    change_record(tmp_path, update)
    with pytest.raises(ValueError, match="extension hash"):
        main.read_case(tmp_path)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_ir_and_actual_query_must_match_even_if_new_hash_is_supplied(tmp_path, version):
    main.save_case(tmp_path, case(), {}, {}, format_version=version)
    def update(record):
        record["case"]["query_ir"]["a"]["query"]["pattern"]["left"]["properties"][0][1]["value"] = 99
        complete = deepcopy(record["case"])
        complete["setup"] = list(main.split_statements((tmp_path / "setup.cypher").read_text()))
        for side in ("a", "b"):
            complete[f"query_{side}"] = main.split_statements((tmp_path / f"query_{side}.cypher").read_text())[0]
        record["input_hash"] = main.case_digest(complete, version)
        record["extension_hash"] = main.extension_digest(complete)
    change_record(tmp_path, update)
    with pytest.raises(ValueError, match="compiled query differs"):
        main.read_case(tmp_path)


def test_recomputed_extension_hash_does_not_allow_invalid_ir(tmp_path):
    main.save_case(tmp_path, case(), {}, {}, format_version=2)
    def update(record):
        record["case"]["query_ir"]["a"]["query"]["returns"][0]["variable"] = "unbound"
        record["extension_hash"] = main.extension_digest(record["case"])
    change_record(tmp_path, update)
    with pytest.raises(QueryError, match="Unbound"):
        main.read_case(tmp_path)


def test_physical_output_mapping_is_checked_against_ir(tmp_path):
    main.save_case(tmp_path, case("ladybug"), {}, {}, format_version=2)
    def update(record):
        record["case"]["output_columns"]["a"][0][1] = "wrong"
        record["extension_hash"] = main.extension_digest(record["case"])
    change_record(tmp_path, update)
    with pytest.raises(ValueError, match="compiled metadata differ"):
        main.read_case(tmp_path)


@pytest.mark.parametrize("version", [1, 2])
def test_shared_ir_legacy_archive_does_not_parse_native_dialect_as_generic_cypher(tmp_path, version):
    original = case("ladybug", "P3")
    assert "* TRAIL" in original["query_a"]
    main.save_case(tmp_path, original, {}, {}, format_version=version)
    assert main.read_case(tmp_path)[0] == original

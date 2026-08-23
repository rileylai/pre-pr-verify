import json
from pathlib import Path

import pytest

from pre_pr_verify.models import ChangeSet, LegacyChangeSet, load_changeset
from pre_pr_verify.schema import render_changeset_schema, render_legacy_changeset_schema


def test_checked_in_legacy_changeset_schema_has_no_drift() -> None:
    path = Path("schemas/changeset-1.0.0.schema.json")

    assert path.read_text() == render_legacy_changeset_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == "ChangeSet"
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "changeset"


def test_checked_in_current_changeset_schema_has_no_drift() -> None:
    path = Path("schemas/changeset-1.1.0.schema.json")

    assert path.read_text() == render_changeset_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == ChangeSet.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.1.0"
    assert "explicit_includes" in schema["properties"]


def test_frozen_legacy_fixture_loads_without_migration() -> None:
    fixture = Path("tests/fixtures/changeset-1.0.0-empty.json")
    loaded = load_changeset(fixture.read_bytes())

    assert isinstance(loaded, LegacyChangeSet)
    assert loaded.schema_version == "1.0.0"
    assert loaded.identity == "630376e6b9563cefb45de48e6373d0c200a9896154a0b64fcbcdea61d6172110"


def test_unknown_changeset_version_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="unsupported changeset schema version"):
        load_changeset({"schema_version": "2.0.0"})

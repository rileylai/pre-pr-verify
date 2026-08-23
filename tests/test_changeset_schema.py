import json
from pathlib import Path

from pre_pr_verify.models import ChangeSet
from pre_pr_verify.schema import render_changeset_schema


def test_checked_in_changeset_schema_has_no_drift() -> None:
    path = Path("schemas/changeset-1.0.0.schema.json")

    assert path.read_text() == render_changeset_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == ChangeSet.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "changeset"


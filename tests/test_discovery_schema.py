import json
from pathlib import Path

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.schema import render_discovery_schema


def test_checked_in_discovery_schema_has_no_drift() -> None:
    path = Path("schemas/discovery-1.0.0.schema.json")

    assert path.read_text() == render_discovery_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == DiscoveryResult.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "discovery"
    assert schema["$defs"]["RequirementResolutionStatus"]["enum"] == [
        "candidates",
        "missing",
    ]
    assert (
        "selected_source_id"
        not in schema["$defs"]["RequirementResolution"]["properties"]
    )

import json
from pathlib import Path

from pre_pr_verify.review_models import ReviewArtifact
from pre_pr_verify.schema import render_review_artifact_schema


def test_checked_in_review_artifact_schema_has_no_drift() -> None:
    path = Path("schemas/review-artifact-1.0.0.schema.json")

    assert path.read_text() == render_review_artifact_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == ReviewArtifact.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "review_artifact"
    assert schema["$defs"]["ReviewVerdict"]["enum"] == [
        "READY",
        "NEEDS_CHANGES",
        "INCONCLUSIVE",
    ]
    assert schema["properties"]["checks"]["maxItems"] == 256
    assert schema["properties"]["evidence_gaps"]["maxItems"] == 256
    assert "check_index" in schema["properties"]
    assert "evidence_gap_index" in schema["properties"]

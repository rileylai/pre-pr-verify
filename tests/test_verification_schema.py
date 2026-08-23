import json
from pathlib import Path

from pre_pr_verify.schema import render_verification_evidence_schema
from pre_pr_verify.verification_models import VerificationEvidence


def test_checked_in_verification_evidence_schema_has_no_drift() -> None:
    path = Path("schemas/verification-evidence-1.0.0.schema.json")

    assert path.read_text() == render_verification_evidence_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == VerificationEvidence.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "verification_evidence"
    assert schema["$defs"]["SnapshotKind"]["enum"] == ["regular", "symlink"]
    assert schema["$defs"]["CheckKind"]["enum"] == [
        "structural_invariant",
        "command",
    ]
    assert "source_preservation_failures" in schema["properties"]
    assert "SourcePreservationFailure" in schema["$defs"]

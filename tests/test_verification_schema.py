import json
from pathlib import Path

from pre_pr_verify.schema import (
    render_verification_evidence_schema,
    render_verification_plan_schema,
)
from pre_pr_verify.verification_models import VerificationEvidence, VerificationPlan


def test_checked_in_verification_evidence_schema_has_no_drift() -> None:
    path = Path("schemas/verification-evidence-1.1.0.schema.json")

    assert path.read_text() == render_verification_evidence_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == VerificationEvidence.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.1.0"
    assert schema["properties"]["contract"]["const"] == "verification_evidence"
    assert schema["$defs"]["SnapshotKind"]["enum"] == ["regular", "symlink"]
    assert schema["$defs"]["CheckKind"]["enum"] == [
        "structural_invariant",
        "command",
    ]
    assert "source_preservation_failures" in schema["properties"]
    assert "SourcePreservationFailure" in schema["$defs"]
    assert "object_format" in schema["$defs"]["SnapshotManifest"]["properties"]


def test_checked_in_verification_plan_schema_has_no_drift() -> None:
    path = Path("schemas/verification-plan-1.1.0.schema.json")

    assert path.read_text() == render_verification_plan_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == VerificationPlan.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.1.0"
    assert "environment_profile" in schema["$defs"]["PlannedCheck"]["properties"]
    assert "profile_provenance" in schema["$defs"]["PlannedCheck"]["properties"]


def test_frozen_legacy_evidence_schema_remains_1_0() -> None:
    schema = json.loads(Path("schemas/verification-evidence-1.0.0.schema.json").read_text())

    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert "environment_profile" not in schema["properties"]

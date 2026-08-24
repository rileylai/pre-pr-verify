import json
from pathlib import Path

from pre_pr_verify.schema import render_semantic_assessment_schema
from pre_pr_verify.semantic_models import SemanticAssessment


def test_checked_in_semantic_assessment_schema_has_no_drift() -> None:
    path = Path("schemas/semantic-assessment-1.0.0.schema.json")

    assert path.read_text() == render_semantic_assessment_schema()
    schema = json.loads(path.read_text())
    assert schema["title"] == SemanticAssessment.__name__
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    assert schema["properties"]["contract"]["const"] == "semantic_assessment"
    assert schema["$defs"]["SemanticAxis"]["enum"] == [
        "spec",
        "standards",
        "impact",
        "test_sufficiency",
        "contextual_security",
    ]
    assert "SemanticEvidenceReference" in schema["$defs"]
    assert "RequirementComparison" in schema["$defs"]
    assert "SemanticLimitGap" in schema["$defs"]
    assert "SemanticReferenceSet" in schema["$defs"]
    reference_index = schema["$defs"]["SemanticReferenceIndex"]["properties"]
    assert reference_index["changed_paths"]["$ref"].endswith("SemanticReferenceSet")
    finding_id = schema["$defs"]["SemanticFinding"]["properties"]["finding_id"]
    assert finding_id["maxLength"] == 128
    comparison_source = schema["$defs"]["RequirementComparison"]["properties"][
        "source_ids"
    ]["items"]
    assert comparison_source["minLength"] == comparison_source["maxLength"] == 64

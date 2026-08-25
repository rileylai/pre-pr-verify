from __future__ import annotations

import json
from pathlib import Path

from pre_pr_verify import __version__
from pre_pr_verify.schema import (
    render_changeset_schema,
    render_discovery_schema,
    render_legacy_changeset_schema,
    render_review_artifact_schema,
    render_semantic_assessment_schema,
    render_verification_evidence_schema,
    render_verification_plan_schema,
)


def test_all_v1_schemas_are_available_from_checkout_and_installed_core() -> None:
    renderers = {
        "changeset-1.0.0.schema.json": render_legacy_changeset_schema,
        "changeset-1.1.0.schema.json": render_changeset_schema,
        "discovery-1.0.0.schema.json": render_discovery_schema,
        "verification-plan-1.1.0.schema.json": render_verification_plan_schema,
        "verification-evidence-1.1.0.schema.json": render_verification_evidence_schema,
        "semantic-assessment-1.0.0.schema.json": render_semantic_assessment_schema,
        "review-artifact-1.0.0.schema.json": render_review_artifact_schema,
    }

    for filename, renderer in renderers.items():
        rendered = renderer()
        assert json.loads(rendered)
        assert Path("schemas", filename).read_text() == rendered


def test_root_skill_is_small_and_routes_to_existing_contracts() -> None:
    skill = Path("SKILL.md")
    content = skill.read_text()
    required_references = [
        "docs/02_review_and_verdict_contracts.md",
        "docs/03_verification_strategy.md",
        "docs/04_security_and_trust.md",
        "docs/05_repository_scope_and_changeset.md",
        "docs/08_development_validation_and_self_hosting.md",
        "docs/09_v1_skill_runbook.md",
    ]

    assert content.startswith("---\nname: pre-pr-verify\n")
    assert len(content) < 6_000
    assert all(reference in content and Path(reference).is_file() for reference in required_references)


def test_skill_runbook_routes_every_canonical_stage_and_policy_boundary() -> None:
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()
    required_calls = {
        "capture_changeset",
        "discover_review_sources",
        "discover_canonical_checks",
        "build_verification_plan",
        "ExecutionCapability",
        "EvidenceReferenceKind",
        "FindingCategory",
        "FindingSeverity",
        "FindingState",
        "RequirementComparison",
        "RequirementRelation",
        "SemanticAxis",
        "SemanticLimitGap",
        "SemanticStatus",
        "execute_verification_plan",
        "bind_semantic_reference",
        "build_semantic_assessment",
        "load_semantic_assessment",
        "build_review_artifact",
        "load_review_artifact",
        "render_markdown_report",
        "verdict_exit_code",
        "discover_scope_options",
        "recommend_scope",
        "resolve_scope_selection",
        "build_scope_preview",
        "capture_resolved_scope",
        "PreReviewSetup",
        "require_ready_to_review",
        "installed_core_identity",
    }

    assert all(name in runbook for name in required_calls)
    assert "nothing_to_review" in runbook
    assert "approved_gaps" in runbook and "explicit" in runbook
    assert "reporting failure" in runbook.lower()
    assert "uv run python" in runbook
    assert "required_capabilities = (CapabilityName.OUTPUT_LIMITS,)" in runbook
    assert "Do not infer optional requirements" in runbook
    assert "complete `CapabilityName`" in runbook
    assert "conservatively require all four" not in runbook
    assert "core-sha256" in runbook or "installed_core_identity" in runbook
    assert "recommend != infer" in runbook
    assert "interactive=False" in runbook
    assert "review_focus" in runbook


def test_numeric_setup_forward_path_covers_representative_narrow_review() -> None:
    skill = Path("SKILL.md").read_text()
    runbook = Path("docs/09_v1_skill_runbook.md").read_text()
    combined = f"{skill}\n{runbook}"

    # A normal narrow dogfood can be completed with numeric setup choices and
    # one final affirmative confirmation, rather than copied scope labels.
    setup_answers = ("1", "1", "1", "yes")
    assert all(answer in combined for answer in setup_answers)
    assert "1. Working changes" in combined
    assert "2. Current branch" in combined
    assert "3. Since commit" in combined
    assert "4. Custom" in combined
    assert "second bounded numbered menu" in combined
    assert "Enter is also valid" in combined

    requirement_section = runbook[runbook.index("### Requirement setup") :]
    verification_section = runbook[runbook.index("### Verification authorization setup") :]
    assert "accept one discovered winning source" in requirement_section
    assert "explicit acceptance criteria" in requirement_section
    assert "Spec will remain `INCONCLUSIVE`" in requirement_section
    assert "implementation code" in requirement_section
    assert "1. explicitly authorize the proposed local checks" in verification_section
    assert "2. review without execution" in verification_section
    assert "3. customize authorization" in verification_section
    assert "approved_gaps" in verification_section
    assert "approval_waivable" in verification_section
    assert "network off" in verification_section
    assert "external services off" in verification_section
    assert "summarize" in verification_section
    assert "interactive=False" in verification_section
    assert "never call an input function or wait" in verification_section
    assert "later patch" not in runbook


def test_runtime_has_no_developer_path_or_provider_key_dependency() -> None:
    runtime = "\n".join(
        path.read_text() for path in sorted(Path("src/pre_pr_verify").glob("*.py"))
    )

    assert "/Users/rileylai" not in runtime
    assert "OPENAI_API_KEY" not in runtime
    assert "ANTHROPIC_API_KEY" not in runtime
    assert "CODEX_API_KEY" not in runtime


def test_current_version_and_documented_boundaries() -> None:
    readme = Path("README.md").read_text()

    assert __version__ == "0.1.5"
    assert Path(".python-version").read_text().strip() == "3.12.13"
    assert "Semantic review, ReviewArtifact reduction/reporting, and GitHub integration remain unimplemented" not in readme
    assert "No `.env`" in readme
    assert "No GitHub MCP" in readme

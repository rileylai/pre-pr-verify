from __future__ import annotations

import pytest

from pre_pr_verify.errors import PreflightError
from pre_pr_verify.pre_review_setup import (
    PreReviewSetup,
    RequirementCandidate,
    SetupPhase,
)


def candidates(count: int) -> list[RequirementCandidate]:
    return [
        RequirementCandidate(source_id=f"{index:064x}", label=f"Requirement {index}")
        for index in range(count)
    ]


def test_numeric_setup_advances_one_required_phase_at_a_time() -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )

    assert setup.phase is SetupPhase.SCOPE
    assert [choice.number for choice in setup.current_step().choices] == [1, 2, 3, 4]

    setup.submit("1")
    assert setup.phase is SetupPhase.REQUIREMENTS
    with pytest.raises(PreflightError, match="not complete"):
        setup.require_ready_to_review()

    setup.submit("1")
    assert setup.phase is SetupPhase.VERIFICATION
    with pytest.raises(PreflightError, match="not complete"):
        setup.require_ready_to_review()

    setup.submit("1")
    assert setup.phase is SetupPhase.FINAL_CONFIRMATION
    with pytest.raises(PreflightError, match="not complete"):
        setup.require_ready_to_review()

    setup.submit("yes")
    assert setup.phase is SetupPhase.READY_TO_REVIEW


def test_enter_confirms_only_an_exposed_default_and_invalid_numbers_fail() -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )
    setup.submit("")
    assert setup.scope_selection == "working-changes"

    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("")
    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("99")


@pytest.mark.parametrize("cancel_phase", list(SetupPhase)[:4])
def test_cancellation_stops_setup_without_a_review_verdict(
    cancel_phase: SetupPhase,
) -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )
    if cancel_phase is not SetupPhase.SCOPE:
        setup.submit("1")
    if cancel_phase in {SetupPhase.VERIFICATION, SetupPhase.FINAL_CONFIRMATION}:
        setup.submit("1")
    if cancel_phase is SetupPhase.FINAL_CONFIRMATION:
        setup.submit("2")

    setup.cancel()
    assert setup.phase is SetupPhase.CANCELLED
    assert setup.review_started is False
    with pytest.raises(PreflightError, match="cancelled"):
        setup.require_ready_to_review()


def test_headless_missing_inputs_never_invoke_a_prompt() -> None:
    setup = PreReviewSetup(
        interactive=False,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )

    with pytest.raises(PreflightError, match="headless.*scope"):
        setup.submit(None)
    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("")
    assert setup.phase is SetupPhase.SCOPE


def test_enter_is_rejected_in_headless_mode_at_every_setup_phase() -> None:
    setup = PreReviewSetup(
        interactive=False,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )
    setup.submit("1", detail="working scope")
    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("")
    setup.submit("1")
    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("")
    setup.submit("1")
    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit("")
    assert setup.phase is SetupPhase.FINAL_CONFIRMATION


@pytest.mark.parametrize("answer", ["9" * 5_000, "not-a-number"])
def test_malformed_or_giant_numeric_selection_is_a_bounded_setup_error(
    answer: str,
) -> None:
    setup = PreReviewSetup(
        interactive=False,
        requirement_candidates=candidates(1),
        recommended_scope_number=1,
    )

    with pytest.raises(PreflightError, match="numbered choice"):
        setup.submit(answer)
    assert setup.phase is SetupPhase.SCOPE


def test_requirement_choices_are_configured_only_after_scope_discovery() -> None:
    setup = PreReviewSetup(interactive=True, recommended_scope_number=1)
    setup.submit("1")
    with pytest.raises(PreflightError, match="discovery"):
        setup.current_step()

    setup.set_requirement_candidates(candidates(1))
    assert setup.current_step().candidate_count == 1
    with pytest.raises(PreflightError, match="configured once"):
        setup.set_requirement_candidates(candidates(1))


def test_thirty_four_candidates_are_reported_without_truncation_or_auto_selection() -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=candidates(34),
        recommended_scope_number=1,
    )
    setup.submit("1")
    step = setup.current_step()

    assert step.phase is SetupPhase.REQUIREMENTS
    assert step.candidate_count == 34
    assert step.candidate_overflow is True
    assert step.presented_candidates == ()
    assert [choice.value for choice in step.choices] == [
        "provide-requirement",
        "continue-without-requirements",
        "cancel",
    ]

    setup.submit("1", detail="The change must preserve the public API.")
    assert setup.phase is SetupPhase.VERIFICATION
    assert setup.requirement_selection == "provided-requirement"


def test_recommended_candidates_are_bounded_and_keep_the_full_candidate_set() -> None:
    values = candidates(34)
    recommended = [
        values[17].source_id,
        values[2].source_id,
        values[29].source_id,
        values[6].source_id,
        values[24].source_id,
    ]
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=values,
        recommended_source_ids=recommended,
        recommended_scope_number=1,
    )

    setup.submit("1")
    step = setup.current_step()

    assert step.candidate_count == 34
    assert step.candidate_overflow is True
    assert step.recommended_candidate_count == 5
    assert step.other_candidate_count == 29
    assert [candidate.source_id for candidate in step.presented_candidates] == recommended
    assert all(candidate.recommended for candidate in step.presented_candidates)
    assert [choice.value for choice in step.choices[:5]] == [
        f"accept:{source_id}" for source_id in recommended
    ]
    assert "[Recommended]" in step.choices[0].label

    setup.submit("1")
    assert setup.requirement_selection == "accepted-discovered-source"
    assert setup.requirement_detail == recommended[0]
    assert tuple(candidate.source_id for candidate in setup.requirement_candidates) == tuple(
        candidate.source_id for candidate in values
    )


def test_headless_recommendation_never_becomes_an_implicit_selection() -> None:
    values = candidates(34)
    setup = PreReviewSetup(
        interactive=False,
        requirement_candidates=values,
        recommended_source_ids=(values[0].source_id,),
    )
    setup.submit("1", detail="working scope")

    assert setup.requirement_selection is None
    assert setup.current_step().presented_candidates[0].recommended is True
    with pytest.raises(PreflightError, match="headless.*requirements"):
        setup.submit(None)
    assert setup.requirement_selection is None


def test_provided_requirement_requires_brief_criteria() -> None:
    setup = PreReviewSetup(
        interactive=True,
        requirement_candidates=candidates(34),
        recommended_scope_number=1,
    )
    setup.submit("1")
    with pytest.raises(PreflightError, match="criteria"):
        setup.submit("1")
    assert setup.phase is SetupPhase.REQUIREMENTS

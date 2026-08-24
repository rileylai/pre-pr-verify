from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pre_pr_verify.errors import PreReviewSetupError, PreflightError
from pre_pr_verify.scope_intent import ResolvedScope, resolved_scope_identity


MAX_PRESENTED_REQUIREMENT_CANDIDATES: Final = 16
MAX_REQUIREMENT_CANDIDATES: Final = 256
MAX_SETUP_DETAIL_CHARS: Final = 4_096
MAX_CHOICE_LABEL_CHARS: Final = 256


class SetupPhase(StrEnum):
    SCOPE = "scope"
    REQUIREMENTS = "requirements"
    VERIFICATION = "verification"
    FINAL_CONFIRMATION = "final_confirmation"
    READY_TO_REVIEW = "ready_to_review"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class NumberedChoice:
    number: int
    label: str
    value: str

    def __post_init__(self) -> None:
        if (
            self.number < 1
            or not self.value
            or not 0 < len(self.label) <= MAX_CHOICE_LABEL_CHARS
        ):
            raise ValueError("numbered setup choice is outside its bound")


@dataclass(frozen=True)
class RequirementCandidate:
    source_id: str
    label: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_id) or not (
            0 < len(self.label) <= MAX_CHOICE_LABEL_CHARS
        ):
            raise ValueError("requirement candidate identity and label are required")


@dataclass(frozen=True)
class SetupStep:
    phase: SetupPhase
    choices: tuple[NumberedChoice, ...]
    default_number: int | None = None
    presented_candidates: tuple[RequirementCandidate, ...] = ()
    candidate_count: int = 0
    candidate_overflow: bool = False


_SCOPE_CHOICES = (
    NumberedChoice(1, "Working changes", "working-changes"),
    NumberedChoice(2, "Current branch", "current-branch"),
    NumberedChoice(3, "Since commit", "since-commit"),
    NumberedChoice(4, "Custom", "custom"),
)
_VERIFICATION_CHOICES = (
    NumberedChoice(1, "Authorize proposed local checks", "authorize"),
    NumberedChoice(2, "Review without execution", "review-without-execution"),
    NumberedChoice(3, "Customize authorization", "customize-authorization"),
    NumberedChoice(4, "Cancel", "cancel"),
)
_FINAL_CHOICES = (
    NumberedChoice(1, "Yes, begin canonical review", "yes"),
    NumberedChoice(2, "Cancel", "cancel"),
)


class PreReviewSetup:
    """Deterministic state and legal transitions for pre-review setup.

    Rendering, prompting, ChangeSet capture, discovery, authorization policy,
    and semantic review remain with their existing owners.
    """

    def __init__(
        self,
        *,
        interactive: bool,
        requirement_candidates: list[RequirementCandidate]
        | tuple[RequirementCandidate, ...]
        | None = None,
        recommended_scope_number: int | None = None,
    ) -> None:
        candidates = tuple(requirement_candidates or ())
        if len(candidates) > MAX_REQUIREMENT_CANDIDATES:
            raise ValueError("requirement candidate set exceeds the setup bound")
        if len({candidate.source_id for candidate in candidates}) != len(candidates):
            raise ValueError("requirement candidate identities must be unique")
        if recommended_scope_number is not None and recommended_scope_number not in {
            choice.number for choice in _SCOPE_CHOICES
        }:
            raise ValueError("recommended scope must identify a bounded scope choice")
        self.interactive = interactive
        self.requirement_candidates = candidates
        self._requirements_configured = requirement_candidates is not None
        self.recommended_scope_number = recommended_scope_number
        self._phase = SetupPhase.SCOPE
        self.scope_selection: str | None = None
        self.scope_detail: str | None = None
        self.requirement_selection: str | None = None
        self.requirement_detail: str | None = None
        self.verification_selection: str | None = None
        self.verification_detail: str | None = None
        self._confirmed_scope: ResolvedScope | None = None
        self._confirmed_scope_identity: str | None = None

    @property
    def review_started(self) -> bool:
        return False

    @property
    def phase(self) -> SetupPhase:
        return self._phase

    def _requirement_step(self) -> SetupStep:
        if not self._requirements_configured:
            raise PreReviewSetupError(
                "requirement discovery must configure setup before selection"
            )
        count = len(self.requirement_candidates)
        if count > MAX_PRESENTED_REQUIREMENT_CANDIDATES:
            choices: tuple[NumberedChoice, ...] = (
                NumberedChoice(1, "Enter explicit acceptance criteria", "provide-requirement"),
                NumberedChoice(
                    2,
                    "Continue without authoritative requirements (Spec INCONCLUSIVE)",
                    "continue-without-requirements",
                ),
                NumberedChoice(3, "Cancel", "cancel"),
            )
            return SetupStep(
                phase=self.phase,
                choices=choices,
                candidate_count=count,
                candidate_overflow=True,
            )

        candidate_choices = tuple(
            NumberedChoice(index, candidate.label, f"accept:{candidate.source_id}")
            for index, candidate in enumerate(self.requirement_candidates, start=1)
        )
        next_number = len(candidate_choices) + 1
        choices = candidate_choices + (
            NumberedChoice(next_number, "Enter explicit acceptance criteria", "provide-requirement"),
            NumberedChoice(
                next_number + 1,
                "Continue without authoritative requirements (Spec INCONCLUSIVE)",
                "continue-without-requirements",
            ),
            NumberedChoice(next_number + 2, "Cancel", "cancel"),
        )
        return SetupStep(
            phase=self.phase,
            choices=choices,
            presented_candidates=self.requirement_candidates,
            candidate_count=count,
        )

    def set_requirement_candidates(
        self,
        candidates: list[RequirementCandidate] | tuple[RequirementCandidate, ...],
    ) -> None:
        if self.phase is not SetupPhase.REQUIREMENTS or self._requirements_configured:
            raise PreReviewSetupError(
                "requirement candidates can be configured once in requirements phase"
            )
        values = tuple(candidates)
        if len(values) > MAX_REQUIREMENT_CANDIDATES:
            raise ValueError("requirement candidate set exceeds the setup bound")
        if len({candidate.source_id for candidate in values}) != len(values):
            raise ValueError("requirement candidate identities must be unique")
        self.requirement_candidates = values
        self._requirements_configured = True

    def current_step(self) -> SetupStep:
        if self.phase is SetupPhase.SCOPE:
            return SetupStep(
                phase=self.phase,
                choices=_SCOPE_CHOICES,
                default_number=self.recommended_scope_number,
            )
        if self.phase is SetupPhase.REQUIREMENTS:
            return self._requirement_step()
        if self.phase is SetupPhase.VERIFICATION:
            return SetupStep(phase=self.phase, choices=_VERIFICATION_CHOICES)
        if self.phase is SetupPhase.FINAL_CONFIRMATION:
            return SetupStep(phase=self.phase, choices=_FINAL_CHOICES)
        return SetupStep(phase=self.phase, choices=())

    def _choice(self, answer: str | int | None) -> NumberedChoice:
        step = self.current_step()
        if answer is None:
            mode = "headless" if not self.interactive else "interactive"
            raise PreReviewSetupError(
                f"{mode} setup requires an explicit {self.phase.value} selection"
            )
        normalized = str(answer).strip()
        if not normalized:
            if not self.interactive:
                raise PreReviewSetupError(
                    f"headless setup requires an explicit bounded numbered choice "
                    f"for {self.phase.value}"
                )
            if step.default_number is None:
                raise PreReviewSetupError(
                    f"{self.phase.value} requires a bounded numbered choice"
                )
            normalized = str(step.default_number)
        if self.phase is SetupPhase.FINAL_CONFIRMATION and normalized.casefold() == "yes":
            normalized = "1"
        if not normalized.isdecimal():
            raise PreReviewSetupError(
                f"{self.phase.value} requires a bounded numbered choice"
            )
        try:
            number = int(normalized)
        except ValueError as error:
            raise PreReviewSetupError(
                f"{self.phase.value} requires a bounded numbered choice"
            ) from error
        for choice in step.choices:
            if choice.number == number:
                return choice
        raise PreReviewSetupError(
            f"{self.phase.value} requires a bounded numbered choice"
        )

    @staticmethod
    def _detail(value: str | None, label: str) -> str:
        if value is None or not value.strip():
            raise PreReviewSetupError(f"{label} requires explicit detail")
        detail = value.strip()
        if len(detail) > MAX_SETUP_DETAIL_CHARS:
            raise PreReviewSetupError(f"{label} detail exceeds the setup bound")
        return detail

    def submit(self, answer: str | int | None, *, detail: str | None = None) -> None:
        if self.phase in {SetupPhase.READY_TO_REVIEW, SetupPhase.CANCELLED}:
            raise PreReviewSetupError(f"setup is already {self.phase.value}")
        choice = self._choice(answer)
        if choice.value == "cancel":
            self.cancel()
            return

        if self.phase is SetupPhase.SCOPE:
            if choice.value != "working-changes":
                self.scope_detail = self._detail(detail, choice.label)
            self.scope_selection = choice.value
            self._phase = SetupPhase.REQUIREMENTS
            return

        if self.phase is SetupPhase.REQUIREMENTS:
            if choice.value.startswith("accept:"):
                self.requirement_selection = "accepted-discovered-source"
                self.requirement_detail = choice.value.removeprefix("accept:")
            elif choice.value == "provide-requirement":
                self.requirement_selection = "provided-requirement"
                self.requirement_detail = self._detail(detail, "provided requirement criteria")
            else:
                self.requirement_selection = choice.value
            self._phase = SetupPhase.VERIFICATION
            return

        if self.phase is SetupPhase.VERIFICATION:
            if choice.value == "customize-authorization":
                self.verification_detail = self._detail(detail, "custom authorization")
            self.verification_selection = choice.value
            self._phase = SetupPhase.FINAL_CONFIRMATION
            return

        if self.phase is SetupPhase.FINAL_CONFIRMATION:
            self._phase = SetupPhase.READY_TO_REVIEW

    def cancel(self) -> None:
        if self.phase is SetupPhase.READY_TO_REVIEW:
            raise PreReviewSetupError("ready setup cannot be cancelled retroactively")
        self._phase = SetupPhase.CANCELLED

    def bind_scope(self, resolved_scope: ResolvedScope) -> None:
        """Bind the explicitly confirmed scope without recapturing it."""

        if self.phase is not SetupPhase.REQUIREMENTS:
            raise PreReviewSetupError(
                "scope identity can be bound only after scope confirmation"
            )
        if self.scope_selection != resolved_scope.intent.value:
            raise PreReviewSetupError(
                "resolved scope does not match the confirmed setup selection"
            )
        if self._confirmed_scope is not None:
            raise PreReviewSetupError("scope identity was already bound")
        self._confirmed_scope = resolved_scope
        self._confirmed_scope_identity = resolved_scope_identity(resolved_scope)

    def require_ready_to_review(
        self,
        current_scope: ResolvedScope | None = None,
    ) -> None:
        if self.phase is SetupPhase.CANCELLED:
            raise PreReviewSetupError("pre-review setup was cancelled; no review exists")
        if self.phase is not SetupPhase.READY_TO_REVIEW:
            raise PreReviewSetupError(
                f"pre-review setup is not complete; current phase is {self.phase.value}"
            )
        if self._confirmed_scope is None or self._confirmed_scope_identity is None:
            raise PreReviewSetupError(
                "confirmed scope identity is required before canonical review"
            )
        candidate = current_scope or self._confirmed_scope
        try:
            current_identity = resolved_scope_identity(candidate)
        except PreflightError as error:
            raise PreReviewSetupError(
                "confirmed scope is stale; restart setup"
            ) from error
        if current_identity != self._confirmed_scope_identity:
            raise PreReviewSetupError("confirmed scope is stale; restart setup")

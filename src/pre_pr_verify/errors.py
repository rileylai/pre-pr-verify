class PrePRVerifyError(Exception):
    """Base error for expected PrePR Verify failures."""


class PreflightError(PrePRVerifyError):
    """The repository comparison or deterministic capture could not be formed."""


class ScopeSelectionRequired(PreflightError):
    """An interactive scope choice is required before deterministic capture."""


class ScopeSelectionCancelled(PreflightError):
    """The human cancelled interactive scope setup before a review existed."""


class PreReviewSetupError(PreflightError):
    """Pre-review setup is incomplete, invalid, or cancelled."""


class InternalCaptureError(PrePRVerifyError):
    """The capture contract or tool failed unexpectedly."""


class ReauthorizationRequired(PreflightError):
    """The authorized verification inputs no longer match the current plan."""


class EvidenceReuseError(PrePRVerifyError):
    """Persisted verification evidence cannot be safely reused."""


class ExecutionAuthorizationRequired(PreflightError):
    """Setup did not authorize execution of verification checks."""

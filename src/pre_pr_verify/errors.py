class PrePRVerifyError(Exception):
    """Base error for expected PrePR Verify failures."""


class PreflightError(PrePRVerifyError):
    """The repository comparison or deterministic capture could not be formed."""


class ScopeSelectionRequired(PreflightError):
    """An interactive scope choice is required before deterministic capture."""


class ScopeSelectionCancelled(PreflightError):
    """The human cancelled interactive scope setup before a review existed."""


class InternalCaptureError(PrePRVerifyError):
    """The capture contract or tool failed unexpectedly."""

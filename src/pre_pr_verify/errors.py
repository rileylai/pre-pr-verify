class PrePRVerifyError(Exception):
    """Base error for expected PrePR Verify failures."""


class PreflightError(PrePRVerifyError):
    """The repository comparison or deterministic capture could not be formed."""


class InternalCaptureError(PrePRVerifyError):
    """The capture contract or tool failed unexpectedly."""


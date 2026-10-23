"""Public errors for UtterVerify."""


class UtterVerifyError(Exception):
    """Base error."""


class ConfigError(UtterVerifyError, ValueError):
    """Configuration is invalid."""


class BundleError(UtterVerifyError, ValueError):
    """Bundle input or layout is invalid."""


class HandoffError(UtterVerifyError, ValueError):
    """RenderPlan / VoiceRender handoff is incomplete or inconsistent."""

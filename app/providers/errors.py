class ProviderError(RuntimeError):
    """A sanitized provider failure safe to map to the public API."""


class NoVisualSubjectError(ProviderError):
    """The input was understood but does not describe anything to draw."""

"""Shared removal refusal type, including when the command runs as __main__."""


class Refused(ValueError):
    def __init__(self, code, diagnostic=None):
        self.diagnostic = diagnostic or {}
        self.code = code
        super().__init__(code)


def failure_details(error, stage):
    """Keep only structured metadata and OS numbers, never exception messages."""
    details = {"stage": stage}
    if isinstance(error, Refused):
        details.update(error.diagnostic)
    for key in ("errno", "winerror"):
        value = getattr(error, key, None)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2147483647:
            details[key] = value
    return details

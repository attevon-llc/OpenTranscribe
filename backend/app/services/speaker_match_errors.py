"""Errors raised by retroactive speaker matching."""


class SuggestionSyncIncompleteError(Exception):
    """Matches were committed and indexed, but the suggestion documents could not be loaded.

    Carries the real counts so a caller can report a partial result instead of the
    zeros that mean "nothing matched".
    """

    def __init__(self, auto_applied_count: int, suggested_count: int) -> None:
        super().__init__(
            f"suggestion sync incomplete after {auto_applied_count} auto-applied, "
            f"{suggested_count} suggested"
        )
        self.auto_applied_count = auto_applied_count
        self.suggested_count = suggested_count

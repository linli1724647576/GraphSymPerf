from dataclasses import dataclass, field


class QueryRejected(Exception):
    """A database rejects a query because of a recognized backend requirement."""

    def __init__(self, reason, details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass
class Query:
    """Query text and bindings submitted to one database."""

    text: str
    parameters: dict = field(default_factory=dict)

    # Declared projection order; HTTP does not supply columns for empty results.
    columns: tuple[str, ...] | None = None

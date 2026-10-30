from dataclasses import dataclass, field


class QueryRejected(Exception):
    """A recognized backend requirement rejects this query without establishing equivalence."""

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
    # 声明的投影顺序；HTTP 空结果不提供列元数据。
    columns: tuple[str, ...] | None = None

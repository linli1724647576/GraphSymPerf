"""Worker protocol boundary; implementations own adapter lifecycle."""

from typing import Protocol


class ShardWorker(Protocol):
    def __call__(self, task: object) -> dict: ...

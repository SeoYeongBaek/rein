"""rein: Agent = Model + Harness.

공개 API 표면은 CLAUDE.md §4에서 M1 시점에 확정되며 이후 시그니처를
바꾸지 않는다. 이 파일은 그 표면의 최상위 진입점이다.

    from rein import Harness

    h = Harness(record="run.jsonl")

    @h.register_tool
    def delete_file(path: str):
        ...

    with Harness(record="run.jsonl") as h:
        agent.run(task="...")
"""

from rein.guardrails.exceptions import (
    ApprovalRequired,
    Denied,
    GuardrailVerdictError,
    RetryRequested,
)
from rein.harness import Harness

__all__ = [
    "Harness",
    "GuardrailVerdictError",
    "Denied",
    "RetryRequested",
    "ApprovalRequired",
]
__version__ = "0.1.0"

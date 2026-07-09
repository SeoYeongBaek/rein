"""내장 어댑터의 내부 구현 디테일 (CLAUDE.md §3).

공개 확장 포인트 아님 — §12 M4 "추가 어댑터"에서 별도 설계.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolUse:
    """LLM이 제안한 단일 tool_use (§3 _observe 표면 자료구조)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


def has_extract_tool_calls(obj: Any) -> bool:
    """§3 두 번째 갈래 검사 (duck typing, 호출 가능성까지 검증).

    단순히 속성이 존재하는지만 보지 않고, 실제로 호출 가능한 함수인지까지
    확인한다 — 존재만 하고 호출 불가능한 메서드면 어댑터로 부적합하다.
    """
    if obj is None:
        return False
    method = getattr(obj, "extract_tool_calls", None)
    return callable(method)

"""내장 어댑터의 내부 구현 디테일 + 서드파티 공개 계약 (CLAUDE.md §3).

ModelAdapter/register_adapter는 §12 M4 "추가 어댑터"에서 공개된
서드파티 확장 포인트다. has_extract_tool_calls/ToolUse는 여전히
내장 어댑터의 내부 구현 디테일이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolUse:
    """LLM이 제안한 단일 tool_use (§3 _observe 표면 자료구조)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelAdapter(Protocol):
    """서드파티 어댑터 공식 계약 (#80).

    extract_tool_calls(response) -> list[ToolUse] 하나만 구현하면
    register_adapter()로 등록해 관측 파이프라인에 연결할 수 있다.
    """

    def extract_tool_calls(self, response: Any) -> list[ToolUse]: ...


def has_extract_tool_calls(obj: Any) -> bool:
    """§3 두 번째 갈래 검사 (duck typing, 호출 가능성까지 검증).

    단순히 속성이 존재하는지만 보지 않고, 실제로 호출 가능한 함수인지까지
    확인한다 — 존재만 하고 호출 불가능한 메서드면 어댑터로 부적합하다.
    """
    if obj is None:
        return False
    method = getattr(obj, "extract_tool_calls", None)
    return callable(method)

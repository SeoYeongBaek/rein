"""내장 어댑터의 내부 구현 디테일 (CLAUDE.md §3).

공개 확장 포인트 아님 — §12 M4 "추가 어댑터"에서 별도 설계.
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
class ToolCallExtractor(Protocol):
    """§3 최소 프로토콜. 내장 어댑터의 내부 구현 디테일.

    기존 __init__.py에서 분리. 반환 타입을 list[Any]에서 list[ToolUse]
    로 정밀화 — _observe 산출물의 §9 args 형식과 정직.
    """

    def extract_tool_calls(self, response: Any) -> list[ToolUse]: ...


def has_extract_tool_calls(obj: Any) -> bool:
    """§3 두 번째 갈래 검사 (duck typing).

    런타임에서 extract_tool_calls 메서드 호출 가능 여부만 본다 —
    Protocol 등록/상속 여부와 무관.
    """
    if obj is None:
        return False
    return callable(getattr(obj, "extract_tool_calls", None))

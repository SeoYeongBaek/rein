"""Anthropic 모델 클라이언트 어댑터 (CLAUDE.md §3, 내장).

Anthropic Messages API 응답에서 tool_use 블록을 추출한다. §3 _observe는
기록 전용이라 응답을 가로채 수정하지 않는다.

Anthropic 응답에서 assistant가 제안한 tool_use는
response.content 배열 안의 블록들 중 type=="tool_use" 항목이다. 각
블록은 {type: "tool_use", id, name, input(dict)} 구조다. text 블록은
관측 대상이 아니다.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.protocol import ToolUse


class AnthropicAdapter:
    """§3 내장 Anthropic 어댑터. 단일 메서드만 노출한다."""

    def extract_tool_calls(self, response: Any) -> list[ToolUse]:
        """Messages API 응답에서 tool_use 블록 추출."""
        # content는 SDK에서 list[Any] 또는 attribute 접근 둘 다 가능.
        # dict 형태와 객체 형태 모두 받는다.
        content = _get_content(response)
        if not content:
            return []

        out: list[ToolUse] = []
        for block in content:
            if _get_type(block) != "tool_use":
                continue
            name = _get_field(block, "name", default="")
            input = _get_field(block, "input", default={})
            if not name:
                continue
            args = dict(input) if isinstance(input, dict) else {}
            out.append(ToolUse(name=name, args=args))
        return out


def _get_content(response: Any) -> Any:
    """response.content를 dict/object 모두에서 안전 접근."""
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("content")
    return getattr(response, "content", None)


def _get_type(block: Any) -> str:
    """블록의 type 필드를 dict/object 모두에서 접근."""
    if block is None:
        return ""
    if isinstance(block, dict):
        return str(block.get("type", "")) or ""
    return str(getattr(block, "type", "")) or ""


def _get_field(block: Any, key: str, default: Any) -> Any:
    """블록의 필드를 dict/object 모두에서 안전 접근."""
    if block is None:
        return default
    if isinstance(block, dict):
        val = block.get(key, default)
        return default if val is None else val
    val = getattr(block, key, default)
    return default if val is None else val

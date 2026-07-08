"""OpenAI 모델 클라이언트 어댑터 (CLAUDE.md §3, 내장).

Chat Completions 응답에서 tool_use를 추출한다. §3 _observe는
기록 전용이라 응답을 가로채 수정하지 않는다 — 추출만 한다.

OpenAI Chat Completions 응답에서 assistant가 제안한 tool_calls는
response.choices[0].message.tool_calls 배열에 담긴다. 각 항목은
{id, type: "function", function: {name, arguments(str)}} 구조다.
arguments는 JSON 문자열이므로 파싱해서 dict로 정규화한다 — 파싱
실패 시 빈 dict로 둔다(M2 severity 태우기는 본 PR 스코프 밖).
"""

from __future__ import annotations

import json
from typing import Any

from rein.adapters.protocol import ToolUse


class OpenAIAdapter:
    """§3 내장 OpenAI 어댑터. 단일 메서드만 노출한다."""

    def extract_tool_calls(self, response: Any) -> list[ToolUse]:
        """Chat Completions 응답에서 tool_calls 추출.

        §3 _observe 표면 — 추출만 하고 응답은 건드리지 않는다.
        SDK 응답 객체(dict-like) 모두 받는다.
        """
        choices = _dig(response, ("choices",), default=())
        if not choices:
            return []
        first = choices[0]
        message = _dig(first, ("message",), default=None)
        if message is None:
            return []
        tool_calls = _dig(message, ("tool_calls",), default=())
        if not tool_calls:
            return []

        out: list[ToolUse] = []
        for tc in tool_calls:
            function = _dig(tc, ("function",), default={})
            name = _dig(function, ("name",), default="")
            raw_args = _dig(function, ("arguments",), default="")
            if not name:
                continue
            out.append(ToolUse(name=name, args=_parse_arguments(raw_args)))
        return out


def _dig(obj: Any, path: tuple[str, ...], default: Any) -> Any:
    """dict/객체 모두에서 점 없는 경로 키로 안전 접근."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return default
    return cur


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """arguments 필드는 SDK에서 str(JSON) / dict / None 셋 다 가능."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}
    return {}
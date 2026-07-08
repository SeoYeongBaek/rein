"""모델 어댑터 (CLAUDE.md §3). OpenAI/Claude/로컬 등 프로바이더 비종속화.

CLAUDE.md §3:
- 내장 타입 자동 감지(openai/anthropic 모듈 prefix)와 extract_tool_calls
  최소 프로토콜(duck typing) 두 갈래로 어댑터를 인식한다.
- 이 모듈의 공개 표면은 ToolUse / is_recognized_adapter 두 개이며,
  서드파티 어댑터 등록용 공개 플러그인 경로는 §12 M4에서 별도 설계.
  지금 열면 M1 스코프로 슬며시 들어오는 크리프가 된다.

세부 구현 디테일:
- 내장 타입 자동 감지 모듈 prefix 검사: builtin.py
- ToolUse / Protocol / has_extract_tool_calls: protocol.py
- 내장 어댑터 본체(OpenAIAdapter, AnthropicAdapter, LocalAdapter):
  providers/ — 사용자가 직접 만지지 않는 내부 구현.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.builtin import is_builtin_model_client
from rein.adapters.protocol import (
    ToolCallExtractor,
    ToolUse,
    has_extract_tool_calls,
)

__all__ = ["ToolUse", "is_recognized_adapter"]


def is_recognized_adapter(client: Any) -> bool:
    """§3 어댑터 인식 판정.

    다음 세 갈래 중 하나 만족 시 True:
      (a) 내장 타입 자동 감지 (모듈 prefix: openai/anthropic)
      (b) ToolCallExtractor Protocol 구현 (isinstance 검사)
      (c) extract_tool_calls 메서드 구현 (duck typing)

    (b)/(c)는 의미상 같은 갈래를 두 메커니즘으로 검사하는 것이며, 둘
    중 하나만 만족하면 된다. 둘 다 불만족이면 observe_model() 호출
    시점에 즉시 TypeError — §5 fail-closed와 같은 패턴.
    """
    if client is None:
        return False
    if is_builtin_model_client(client):
        return True
    if isinstance(client, ToolCallExtractor):
        return True
    if has_extract_tool_calls(client):
        return True
    return False

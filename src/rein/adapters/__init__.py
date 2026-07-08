"""모델 어댑터 (CLAUDE.md §3). OpenAI/Claude/로컬 등 프로바이더 비종속화.

CLAUDE.md §3:
- 내장 타입 자동 감지(openai/anthropic 모듈 prefix)와 extract_tool_calls
  최소 프로토콜(duck typing) 두 갈래로 어댑터를 인식한다.
- 자동 감지로 인정된 내장 클라이언트는 위임 헬퍼 extract_tool_calls_for가
  내장 어댑터(OpenAIAdapter / AnthropicAdapter)로 라우팅한다 — 순정
  openai.OpenAI 인스턴스 자체엔 extract_tool_calls 메서드가 없으므로,
  자동 감지 "통과만" 시키고 끝내면 §3 _observe가 무용지물이 된다.
  인식과 라우팅은 짝으로 움직인다.
- 공개 표면: ToolUse, is_recognized_adapter, extract_tool_calls_for.
  서드파티 어댑터 등록용 공개 플러그인 경로는 §12 M4에서 별도 설계.

세부 구현 디테일:
- 내장 타입 자동 감지 모듈 prefix 검사: builtin.py
- ToolUse / Protocol / has_extract_tool_calls: protocol.py
- 내장 어댑터 본체(OpenAIAdapter, AnthropicAdapter, LocalAdapter):
  providers/ — 사용자가 직접 만지지 않는 내부 구현.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.builtin import (
    BUILTIN_ANTHROPIC_PREFIX,
    BUILTIN_OPENAI_PREFIX,
    is_builtin_model_client,
)
from rein.adapters.protocol import (
    ToolCallExtractor,
    ToolUse,
    has_extract_tool_calls,
)
from rein.adapters.providers.anthropic import AnthropicAdapter
from rein.adapters.providers.openai import OpenAIAdapter

__all__ = ["ToolUse", "is_recognized_adapter", "extract_tool_calls_for"]


def _builtin_kind(client: Any) -> str | None:
    """내장 자동 감지 결과 내 종류로 환원. None이면 자동 감지 미해당."""
    module = (type(client).__module__ or "").split(".")[0]
    if module == BUILTIN_OPENAI_PREFIX:
        return "openai"
    if module == BUILTIN_ANTHROPIC_PREFIX:
        return "anthropic"
    return None


def is_recognized_adapter(client: Any) -> bool:
    """§3 어댑터 인식 판정.

    다음 세 갈래 중 하나 만족 시 True:
      (a) 내장 타입 자동 감지 (모듈 prefix: openai/anthropic)
      (b) ToolCallExtractor Protocol 구현 (isinstance 검사)
      (c) extract_tool_calls 메서드 구현 (duck typing)

    둘 다 불만족이면 observe_model() 호출 시점에 즉시 TypeError —
    §5 fail-closed와 같은 패턴.
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


def extract_tool_calls_for(client: Any, response: Any) -> list[ToolUse]:
    """§3 _observe용 단일 추출 진입점.

    client가 자동 감지로 내장으로 인정되면 내장 어댑터로 라우팅한다:
      - openai.* 클라이언트 → OpenAIAdapter
      - anthropic.* 클라이언트 → AnthropicAdapter

    자동 감지에 안 걸리면 client 자신의 extract_tool_calls에 직접 위임.
    로컬은 §3 TODO로 자동 감지에서 빠지므로 이 분기에 자연스럽게 들어온다
    (사용자가 자기 어댑터에 extract_tool_calls를 구현해 둔 경우).

    Args:
        client: observe_model()로 등록된 모델 클라이언트 객체.
        response: SDK가 반환한 모델 응답.

    Returns:
        LLM이 제안한 tool_use 목록. 없으면 빈 리스트.

    Raises:
        TypeError: client가 §3 어느 갈래로도 인식되지 않는 경우 (호출자 책임
            — observe_model() 시점에 이미 한 번 막혔어야 하지만, 위임
            단계에서도 마지막 방어선으로 즉시 실패).
    """
    if not is_recognized_adapter(client):
        raise TypeError(
            f"extract_tool_calls_for: {type(client)!r}는 인식된 어댑터가 아닙니다. "
            "observe_model() 시점에 이미 거부되었어야 합니다."
        )

    kind = _builtin_kind(client)
    if kind == "openai":
        return OpenAIAdapter().extract_tool_calls(response)
    if kind == "anthropic":
        return AnthropicAdapter().extract_tool_calls(response)

    # 내장 자동 감지 미해당 → client 자신의 extract_tool_calls에 위임.
    # §3 최소 프로토콜은 단일 메서드만 요구하므로 그대로 호출.
    return client.extract_tool_calls(response)

"""모델 어댑터 (CLAUDE.md §3). OpenAI/Claude/로컬 등 프로바이더 비종속화.

CLAUDE.md §3:
- 내장 타입 자동 감지(openai/anthropic 모듈 prefix)와 extract_tool_calls
  최소 프로토콜(duck typing) 두 갈래로 어댑터를 인식한다.
- 자동 감지로 인정된 내장 클라이언트는 extract_tool_calls_for가
  내장 어댑터로 라우팅한다.
- 공개 표면: ToolUse, is_recognized_adapter, extract_tool_calls_for.
  서드파티 어댑터 등록용 공개 플러그인 경로는 §12 M4에서 별도 설계.

내부 경로 은닉:
- providers/ 하위 클래스는 공개 표면에서 숨긴다. 직접 접근은 가능하나
  권장하지 않으며, 향후 내부 구현 교체 시에도 외부 코드가 깨지지 않도록
  별칭 매핑으로만 접근을 제한한다.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.builtin import (
    BUILTIN_ANTHROPIC_PREFIX,
    BUILTIN_OPENAI_PREFIX,
    is_builtin_model_client,
)
from rein.adapters.protocol import ToolUse, has_extract_tool_calls
from rein.adapters.providers.anthropic import AnthropicAdapter as _AnthropicAdapter
from rein.adapters.providers.openai import OpenAIAdapter as _OpenAIAdapter

__all__ = ["ToolUse", "is_recognized_adapter", "extract_tool_calls_for"]

# 내부 구현을 단 한 번만 인스턴스화 — extract_tool_calls_for 호출마다
# 새 객체를 만들면 호출 빈번 시 오버헤드가 누적된다(R106).
_OPENAI_ADAPTER = _OpenAIAdapter()
_ANTHROPIC_ADAPTER = _AnthropicAdapter()


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

    다음 두 갈래 중 하나 만족 시 True (§3 명세 그대로):
      (a) 내장 타입 자동 감지 (모듈 prefix: openai/anthropic)
      (b) extract_tool_calls 메서드가 존재하고 호출 가능 (duck typing)

    둘 다 불만족이면 observe_model() 호출 시점에 즉시 TypeError —
    §5 fail-closed와 같은 패턴.
    """
    if client is None:
        return False
    if is_builtin_model_client(client):
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
    """
    if not is_recognized_adapter(client):
        raise TypeError(
            f"extract_tool_calls_for: {type(client)!r}는 인식된 어댑터가 아닙니다. "
            "observe_model() 시점에 이미 거부되었어야 합니다."
        )

    kind = _builtin_kind(client)
    if kind == "openai":
        return _OPENAI_ADAPTER.extract_tool_calls(response)
    if kind == "anthropic":
        return _ANTHROPIC_ADAPTER.extract_tool_calls(response)

    return client.extract_tool_calls(response)

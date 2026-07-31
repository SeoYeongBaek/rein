"""모델 어댑터 (CLAUDE.md §3). OpenAI/Claude/로컬 등 프로바이더 비종속화.

CLAUDE.md §3:
- 내장 타입 자동 감지(openai/anthropic 모듈 prefix)와 extract_tool_calls
  최소 프로토콜(duck typing) 두 갈래로 어댑터를 인식한다.
- 자동 감지로 인정된 내장 클라이언트는 extract_tool_calls_for가
  내장 어댑터로 라우팅한다.
- 공개 표면: ToolUse, ModelAdapter, is_recognized_adapter,
  extract_tool_calls_for, register_adapter (#80, M4).

내부 경로 은닉:
- providers/ 하위 클래스는 공개 표면에서 숨긴다. 직접 접근은 가능하나
  권장하지 않으며, 향후 내부 구현 교체 시에도 외부 코드가 깨지지 않도록
  별칭 매핑으로만 접근을 제한한다.

레지스트리 (#80):
- _ADAPTER_REGISTRY는 module_prefix -> ModelAdapter 딕셔너리다.
  "openai"/"anthropic"은 import 시점에 미리 채워진 항목일 뿐이며,
  register_adapter()로 서드파티 prefix를 추가하면 동일한 인식·라우팅
  경로(is_recognized_adapter/extract_tool_calls_for)를 탄다.
- 자동 몽키패치 배선(_patch_target_for)은 이 레지스트리를 보지
  않는다 — openai/anthropic 두 개만 하드코딩된 _builtin_kind()를
  그대로 쓴다. 서드파티 등록 어댑터의 호출 진입점(메서드명·시그니처)에는
  아무 계약이 없어 자동으로 몽키패치하는 것이 안전하지 않기 때문이다.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.builtin import BUILTIN_ANTHROPIC_PREFIX, BUILTIN_OPENAI_PREFIX
from rein.adapters.protocol import ModelAdapter, ToolUse, has_extract_tool_calls
from rein.adapters.providers.anthropic import AnthropicAdapter as _AnthropicAdapter
from rein.adapters.providers.openai import OpenAIAdapter as _OpenAIAdapter

__all__ = [
    "ToolUse",
    "ModelAdapter",
    "is_recognized_adapter",
    "extract_tool_calls_for",
    "register_adapter",
]

# 내부 구현을 단 한 번만 인스턴스화 — extract_tool_calls_for 호출마다
# 새 객체를 만들면 호출 빈번 시 오버헤드가 누적된다(R106).
_OPENAI_ADAPTER = _OpenAIAdapter()
_ANTHROPIC_ADAPTER = _AnthropicAdapter()

# module_prefix -> ModelAdapter. openai/anthropic은 "먼저 등록된 내장
# 항목"일 뿐 서드파티 등록과 같은 자료구조·경로를 공유한다(#80).
_ADAPTER_REGISTRY: dict[str, ModelAdapter] = {
    BUILTIN_OPENAI_PREFIX: _OPENAI_ADAPTER,
    BUILTIN_ANTHROPIC_PREFIX: _ANTHROPIC_ADAPTER,
}


def register_adapter(module_prefix: str, adapter: ModelAdapter) -> None:
    """서드파티 어댑터를 module_prefix로 등록한다 (#80).

    등록 후에는 사용자가 원시 클라이언트를 그대로 observe_model()에
    넘기면 is_recognized_adapter/extract_tool_calls_for가 자동으로
    인식·라우팅한다. 단, 자동 몽키패치 배선(observe_model의 실제 호출
    메서드 가로채기)은 지원하지 않는다 — 서드파티 클라이언트의 호출
    진입점에는 아무 계약이 없어 임의 속성을 자동으로 덮어쓰는 것이
    안전하지 않다. observe_model()은 _observed_client만 세팅하고,
    사용자가 harness._observe(response)를 직접 호출해야 한다.

    같은 prefix가 '다른' 어댑터로 이미 등록돼 있으면 ValueError
    (fail-closed) — 두 서드파티 패키지의 충돌을 조용히 덮어쓰지 않는다.
    동일 객체(identity)의 재등록(모듈 재import 등)은 idempotent하게
    허용한다.

    등록 시점에 두 가지를 fail-closed로 검증한다(리뷰 finding 1/2,
    #80): module_prefix는 점(.)이 없는 최상위 모듈명 한 토큰이어야
    하고(그렇지 않으면 매칭 로직이 `__module__.split(".")[0]`만 보므로
    영영 매칭되지 않는 죽은 등록이 된다), adapter는 호출 가능한
    extract_tool_calls를 구현해야 한다(그렇지 않으면 실패가 한참 뒤
    fail-open인 _observe 안에서야 조용히 드러난다). 둘 다 위반 시
    즉시 예외 — §5 stage_order 검증과 같은 fail-closed 패턴.
    """
    if not isinstance(module_prefix, str) or not module_prefix or "." in module_prefix:
        raise ValueError(
            "register_adapter: module_prefix는 최상위 모듈명 한 토큰이어야 합니다 "
            f'(예: "vllm"). 받은 값: {module_prefix!r}'
        )
    if not has_extract_tool_calls(adapter):
        raise TypeError(
            f"register_adapter: adapter {adapter!r}는 호출 가능한 "
            "extract_tool_calls(response) 메서드를 구현해야 합니다 (ModelAdapter 계약)."
        )
    existing = _ADAPTER_REGISTRY.get(module_prefix)
    if existing is not None and existing is not adapter:
        raise ValueError(
            f"register_adapter: module_prefix {module_prefix!r}는 이미 다른 "
            f"어댑터로 등록되어 있습니다({existing!r}). 서드파티 패키지 간 "
            "prefix 충돌입니다 — 다른 module_prefix를 쓰거나 충돌하는 "
            "패키지를 확인하세요."
        )
    _ADAPTER_REGISTRY[module_prefix] = adapter


def _registry_kind(client: Any) -> str | None:
    """client의 모듈 prefix가 _ADAPTER_REGISTRY에 등록돼 있으면 그 prefix를 반환.

    is_recognized_adapter/extract_tool_calls_for의 인식·라우팅 판정에만
    쓰인다 — 자동 몽키패치 배선 여부는 이 함수가 아니라 _builtin_kind()로
    별도 판정한다(위 모듈 docstring 참고).
    """
    if client is None:
        return None
    module = (type(client).__module__ or "").split(".")[0]
    return module if module in _ADAPTER_REGISTRY else None


def _builtin_kind(client: Any) -> str | None:
    """자동 배선(몽키패치) 대상 판정 전용. openai/anthropic 두 개만 본다.

    _registry_kind와 의도적으로 분리한다 — 레지스트리에 서드파티가
    추가돼도 이 함수의 판정 범위는 넓어지지 않는다.
    """
    module = (type(client).__module__ or "").split(".")[0]
    if module == BUILTIN_OPENAI_PREFIX:
        return "openai"
    if module == BUILTIN_ANTHROPIC_PREFIX:
        return "anthropic"
    return None


def _patch_target_for(client: Any) -> tuple[Any, str] | None:
    """observe_model 자동 배선(몽키패치) 대상 (owner, attr_name) 조회.

    빌트인(OpenAI/Anthropic)에만 적용한다. duck-typed 커스텀/로컬
    클라이언트나 register_adapter로 등록된 서드파티 클라이언트는 §3 두
    번째 갈래(또는 #80 레지스트리)로 "인식"은 되지만, 실제 호출
    진입점(메서드명, 시그니처, 동기/스트리밍 여부)에 대해서는 아무
    계약도 없어, 임의 객체의 임의 속성을 자동으로 덮어쓰는 것은
    안전하지 않으므로 None을 반환해 자동 배선을 포기한다
    (harness.observe_model이 _observed_client만 세팅하고 패치는
    건너뛴다). 이 경우 사용자가 자기 호출부에서 직접
    harness._observe(response)를 호출해야 한다.
    """
    kind = _builtin_kind(client)
    if kind == "openai":
        owner = getattr(client, "chat", None)
        owner = getattr(owner, "completions", None) if owner is not None else None
    elif kind == "anthropic":
        owner = getattr(client, "messages", None)
    else:
        return None
    if owner is None or not callable(getattr(owner, "create", None)):
        return None  # 예상 구조와 다름 — 방어적으로 자동 배선 포기
    return owner, "create"


def is_recognized_adapter(client: Any) -> bool:
    """§3 어댑터 인식 판정.

    다음 두 갈래 중 하나 만족 시 True:
      (a) _ADAPTER_REGISTRY에 등록된 module_prefix (내장 openai/anthropic
          + register_adapter로 추가된 서드파티, #80)
      (b) extract_tool_calls 메서드가 존재하고 호출 가능 (duck typing)

    둘 다 불만족이면 observe_model() 호출 시점에 즉시 TypeError —
    §5 fail-closed와 같은 패턴.
    """
    if client is None:
        return False
    if _registry_kind(client) is not None:
        return True
    if has_extract_tool_calls(client):
        return True
    return False


def extract_tool_calls_for(client: Any, response: Any) -> list[ToolUse]:
    """§3 _observe용 단일 추출 진입점.

    client의 모듈 prefix가 _ADAPTER_REGISTRY에 있으면(내장 또는
    register_adapter로 등록된 서드파티, #80) 그 어댑터로 라우팅한다.
    레지스트리에 없으면 client 자신의 extract_tool_calls에 직접
    위임한다(duck typing 경로).
    """
    if not is_recognized_adapter(client):
        raise TypeError(
            f"extract_tool_calls_for: {type(client)!r}는 인식된 어댑터가 아닙니다. "
            "observe_model() 시점에 이미 거부되었어야 합니다."
        )

    kind = _registry_kind(client)
    if kind is not None:
        return _ADAPTER_REGISTRY[kind].extract_tool_calls(response)

    return client.extract_tool_calls(response)

# M4 설계: 서드파티 어댑터 공개 플러그인 경로 (#80)

날짜: 2026-07-31
작성: 서영 (브레인스토밍 세션)
관련 마일스톤: CLAUDE.md §12 M4, 이슈 #80
관련 이슈: #77 (로컬 클라이언트 자동 감지 — 별개, 이 스펙 범위 아님)

## 배경

M1~M3는 모두 완료되었다(이슈 #1~#77 전부 closed). M4는 "확장 버킷"
단계이고, 그중 `observe_model`의 어댑터 인식 경로를 서드파티에게
공식적으로 열어주는 작업(#80)이 CLAUDE.md §12에 따라 M4 내에서
최우선 순위다("OSS 채택률에 직결... 3단계 LLM 폴백보다 우선").

현재 CLAUDE.md §3은 다음과 같이 명시한다:

> 이 프로토콜은 **공개 확장 포인트가 아니라 내장 어댑터의 내부 구현
> 디테일**이다. 서드파티가 자기 프로바이더용 어댑터를 등록하는 공개
> 플러그인 경로는 지금 열지 않는다 — §12 M4 "추가 어댑터" 항목에서
> 별도로 설계한다. 지금 열면 M1 스코프로 슬며시 들어오는 크리프가
> 된다.

지금까지는 어댑터 인식이 두 갈래로만 존재했다.

1. **내장 타입 자동 감지** (`adapters/builtin.py::is_builtin_model_client`)
   — `openai`/`anthropic` 모듈 prefix를 `if/elif`로 하드코딩 판정.
2. **최소 프로토콜(duck typing)** (`adapters/protocol.py::has_extract_tool_calls`)
   — `extract_tool_calls(response)` 메서드 존재 여부.

기술적으로는 오늘도 2번 경로로 누구나 자기 클라이언트에
`extract_tool_calls`를 구현해 `observe_model()`에 넘길 수 있다. 하지만
이는 문서상 "공식 확장 포인트"로 명명·보증되지 않은 내부 구현
디테일이며, 자동 인식은 여전히 `openai`/`anthropic` 두 prefix로만
하드코딩되어 있어 서드파티가 만든 어댑터를 rein이 원시 클라이언트만
보고 자동으로 찾아 쓸 방법이 없다.

이번 스펙은 **추상적 공개 프로토콜 설계**가 목적이다. 특정 로컬
런타임(vLLM/Ollama 등, #77) 지원이나 특정 상용 프로바이더(Gemini/
Mistral 등) 추가 자체는 스코프 밖이며, 누구나 따를 수 있는 등록
계약을 만드는 것이 목표다.

## 결정한 것

### 1. 인식 메커니즘을 레지스트리로 통합

현재 `adapters/__init__.py::_builtin_kind()`는 내장 두 개를
`if module == "openai": ... elif module == "anthropic": ...`로
하드코딩한다. 이를 **모듈 prefix → 어댑터 인스턴스 딕셔너리**
(`_ADAPTER_REGISTRY: dict[str, ModelAdapter]`)로 일반화한다.

- 모듈 import 시점에 `"openai"`, `"anthropic"`을 레지스트리에 미리
  채운다. "내장"은 이제 "가장 먼저 등록된 항목"일 뿐이며, 서드파티
  등록과 동일한 자료구조·경로를 공유한다.
- `is_recognized_adapter(client)`와 `extract_tool_calls_for(client,
  response)`는 이 레지스트리 하나만 보고 판정·라우팅한다. 기존
  `if kind == "openai": ... elif kind == "anthropic": ...` 분기는
  딕셔너리 조회로 대체되어 코드도 단순해진다.
- **자동 몽키패치 배선(`_patch_target_for`, `observe_model`의 실제
  호출 메서드 자동 가로채기)은 내장 두 개만 하드코딩으로 유지**한다.
  서드파티가 등록한 어댑터는 `is_recognized_adapter`가 True가 되어
  "인식"되지만, 호출 진입점(메서드명·시그니처·동기/스트리밍 여부)에
  대한 계약이 전혀 없으므로 자동 배선 대상에서는 제외된다.
  `observe_model()`은 `_observed_client`만 세팅하고, 사용자가
  `harness._observe(response)`를 직접 호출해야 한다 — 오늘의
  duck-typed 커스텀 클라이언트 사용자 경험과 완전히 동일하다. 이
  결정은 `_patch_target_for` 기존 문서화된 안전 근거("실제 호출
  진입점에 대해 아무 계약도 없어, 임의 객체의 임의 속성을 자동으로
  덮어쓰는 것은 안전하지 않다")를 그대로 서드파티 어댑터에도 적용한
  것이다.

### 2. 공개 API 추가 (`rein.adapters`)

```python
class ModelAdapter(Protocol):
    """서드파티 어댑터 공식 계약. 기존 duck-typing 계약(has_extract_tool_calls)과
    형태는 동일하나, 공개 타입으로 명명해 문서화·IDE 지원을 받는다."""

    def extract_tool_calls(self, response: Any) -> list[ToolUse]: ...


def register_adapter(module_prefix: str, adapter: ModelAdapter) -> None:
    """module_prefix(예: "vllm")를 가진 클라이언트를 이 adapter로 인식하게
    등록한다. 등록 후 사용자는 원시 클라이언트를 그대로 observe_model()에
    넘기면 자동 인식된다(자동 배선은 아님 — 위 1번 참고).

    같은 prefix가 '다른' 어댑터로 이미 등록돼 있으면 ValueError
    (fail-closed) — 두 서드파티 패키지의 충돌을 조용히 덮어쓰지 않는다.
    동일 객체(identity, `is` 비교)의 재등록(모듈 재import 등)은
    idempotent하게 허용한다. 같은 클래스의 다른 인스턴스는 "다른
    어댑터"로 취급해 에러다 — 값 동등성 비교는 어댑터 객체에 `__eq__`
    구현을 요구하게 되어 계약이 복잡해진다.
    """
```

`__all__`에 `ModelAdapter`, `register_adapter` 추가
(`ToolUse`, `is_recognized_adapter`, `extract_tool_calls_for`와 함께).

**사용 예:**

```python
# 서드파티 패키지(예: rein_vllm_adapter) 내부
from rein.adapters import register_adapter, ModelAdapter

class VLLMAdapter:  # 상속 불필요 — 구조적 만족(Protocol)
    def extract_tool_calls(self, response) -> list[ToolUse]: ...

register_adapter("vllm", VLLMAdapter())
```

```python
# 사용자 코드 — 패키지 import 한 줄이면 등록 끝
import rein_vllm_adapter  # noqa: F401  (등록 side effect)

h = Harness(record="run.jsonl")
h.observe_model(vllm_client)  # 원시 client 그대로, 자동 인식
```

**entry_points/setuptools 기반 자동 탐색은 채택하지 않는다.**
`pip install`만으로 import 없이 인식되게 하는 방식은 "숨은 마법"을
도입해 감사(audit)하기 어렵게 만든다. 사용자가 명시적으로 서드파티
패키지를 `import`해야 등록 side effect가 실행되는 편이 rein 전체의
"조용한 무시/숨은 상태 금지" 원칙(§5 fail-closed, §14)과 일관된다.

### 3. 문서 갱신 (CLAUDE.md §3, living file)

- "서드파티가... 공개 플러그인 경로는 지금 열지 않는다" 서술을
  `register_adapter` 구현 완료 사실로 갱신한다.
- "내장 타입 자동 감지" 표현은 유지하되, 내부적으로 "레지스트리에
  사전 등록된 항목"이라는 점을 명시해 §3 두 갈래 구조(내장 자동 감지
  / 최소 프로토콜)와 `register_adapter`의 관계를 정리한다.
- `adapters/providers/local.py`의 "로컬 클라이언트 자동 감지 기준
  미정, M4에서 별도 설계" TODO는 이번 스펙 범위가 아니므로 그대로
  둔다. `register_adapter`는 일반 프로토콜만 열 뿐, vLLM/Ollama 등
  구체 어댑터를 rein이 기본 내장할지는 별개 결정(#77 연장선)이다.

### 4. 테스트

`tests/test_adapters.py`에 추가할 케이스:

- `register_adapter("fakeprovider", adapter)` 후
  `is_recognized_adapter(client)` / `extract_tool_calls_for`가
  정상 라우팅되는지
- 같은 prefix에 다른 어댑터 재등록 시 `ValueError`
- 같은 prefix에 동일 객체 재등록 시 에러 없이 통과(idempotent)
- 등록된 서드파티 어댑터는 `observe_model()`에서 자동 배선(몽키패치)
  되지 않고 `_observed_client`만 세팅되는지

레지스트리는 프로세스 전역 상태이므로 테스트 간 오염 방지가
필요하다. `unregister_adapter`를 공개 API로 낼지 검토했으나, 그
동기가 테스트 격리 하나뿐이라 공개 표면을 넓히지 않고
`conftest.py`(또는 테스트 모듈)의 fixture가 내부
`_ADAPTER_REGISTRY` 딕셔너리를 직접 정리하는 방식으로 간다
(YAGNI, §10 "자체 구현은 얇게").

## 스코프 밖

- vLLM/Ollama 등 구체 로컬 런타임 어댑터 구현 자체 (#77 연장선, 별도
  이슈)
- entry_points/setuptools 기반 자동 탐색 (명시적 import만 지원)
- 서드파티 어댑터 자동 몽키패치 배선 (안전상 내장 전용 유지)
- `unregister_adapter` 공개 API
- Gemini/Mistral 등 특정 상용 프로바이더 어댑터 신규 구현

## 관련 CLAUDE.md 근거

> §3: "이 프로토콜은 공개 확장 포인트가 아니라 내장 어댑터의 내부
> 구현 디테일이다... 서드파티가 자기 프로바이더용 어댑터를 등록하는
> 공개 플러그인 경로는 지금 열지 않는다 — §12 M4 '추가 어댑터'
> 항목에서 별도로 설계한다."
>
> §12: "여유가 생기면 M4 중에서도 추가 어댑터를 최우선으로 한다(OSS
> 채택률에 직결). 3단계 LLM 폴백보다 우선."

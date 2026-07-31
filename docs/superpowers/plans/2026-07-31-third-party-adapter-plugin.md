# 서드파티 어댑터 공개 플러그인 경로 (#80) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `rein.adapters`에 `register_adapter(module_prefix, adapter)` 공개 API를 추가해, 서드파티가 자기 프로바이더용 어댑터를 등록하면 원시 클라이언트를 그대로 `observe_model()`에 넘겨도 자동 인식(`is_recognized_adapter`)·라우팅(`extract_tool_calls_for`)되게 한다.

**Architecture:** `adapters/__init__.py`의 openai/anthropic 하드코딩 `if/elif` 분기를 `_ADAPTER_REGISTRY: dict[str, ModelAdapter]` 딕셔너리로 일반화한다. import 시점에 `"openai"`/`"anthropic"`을 미리 채워 넣어 내장을 "먼저 등록된 항목"으로 재정의하고, `is_recognized_adapter`/`extract_tool_calls_for`는 이 레지스트리 하나만 조회한다. 자동 몽키패치 배선(`_patch_target_for`)은 안전상 기존처럼 openai/anthropic 두 개만 하드코딩된 `_builtin_kind()`를 계속 쓰고, 레지스트리와는 완전히 분리해 서드파티에는 절대 자동 배선하지 않는다.

**Tech Stack:** Python 3.11+, `typing.Protocol`, pytest.

## Global Constraints

- `Harness`/`register_tool`/CLI 표면은 M1에 고정되어 이 작업으로 변경하지 않는다 (CLAUDE.md §4).
- entry_points/setuptools 기반 자동 탐색은 쓰지 않는다 — 사용자가 서드파티 패키지를 명시적으로 `import`해야 등록된다 (스펙 §2).
- 서드파티 등록 어댑터는 자동 몽키패치 배선 대상이 아니다 — `observe_model()`은 `_observed_client`만 세팅한다 (스펙 §1).
- 같은 `module_prefix`에 '다른' 어댑터가 이미 등록돼 있으면 `ValueError` (fail-closed). 동일 객체(`is` identity) 재등록은 idempotent 허용 (스펙 §2).
- `unregister_adapter` 공개 API는 만들지 않는다 (YAGNI, 스펙 §4).
- 문서는 living file — CLAUDE.md §3을 실제 구현에 맞춰 갱신한다 (CLAUDE.md §14).

---

## Task 1: 레지스트리 인프라 — `ModelAdapter` Protocol + `register_adapter`

**Files:**
- Modify: `src/rein/adapters/protocol.py`
- Modify: `src/rein/adapters/__init__.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: 기존 `ToolUse`(`protocol.py`), `_OPENAI_ADAPTER`/`_ANTHROPIC_ADAPTER` 싱글톤(`adapters/__init__.py:34-35`), `has_extract_tool_calls`(`protocol.py`).
- Produces:
  - `rein.adapters.protocol.ModelAdapter` — `typing.Protocol`, `extract_tool_calls(self, response: Any) -> list[ToolUse]` 메서드 하나.
  - `rein.adapters.register_adapter(module_prefix: str, adapter: ModelAdapter) -> None`
  - `rein.adapters.__all__` == `["ToolUse", "ModelAdapter", "is_recognized_adapter", "extract_tool_calls_for", "register_adapter"]`
  - 내부: `rein.adapters._ADAPTER_REGISTRY: dict[str, ModelAdapter]` (테스트에서 `monkeypatch`로 직접 접근)

### Step 1: `ModelAdapter` Protocol을 실패하는 테스트로 먼저 작성

`tests/test_adapters.py` 맨 위 import를 아래로 교체한다(기존 15번 줄):

```python
from rein.adapters import (
    ModelAdapter,
    ToolUse,
    extract_tool_calls_for,
    is_recognized_adapter,
    register_adapter,
)
```

파일 끝(415번 줄 이후)에 새 섹션을 추가한다:

```python
# ---- register_adapter: 서드파티 등록 (#80) ----


class _ThirdPartyAdapter:
    def __init__(self, tool_name: str = "third_party_tool"):
        self._tool_name = tool_name

    def extract_tool_calls(self, response):
        return [ToolUse(name=self._tool_name, args={})]


def test_model_adapter_protocol_is_structural():
    """ModelAdapter는 명시적 상속 없이도 구조적으로 만족되는 Protocol이어야 한다."""
    assert isinstance(_ThirdPartyAdapter(), ModelAdapter)


def test_register_adapter_makes_prefix_recognized(monkeypatch):
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    class _FakeVLLMClient:
        pass

    _patch_module(_FakeVLLMClient, "vllm.entrypoints")

    adapter = _ThirdPartyAdapter()
    register_adapter("vllm", adapter)

    assert is_recognized_adapter(_FakeVLLMClient()) is True


def test_register_adapter_routes_extract_tool_calls_for(monkeypatch):
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    class _FakeVLLMClient:
        pass

    _patch_module(_FakeVLLMClient, "vllm.entrypoints")

    register_adapter("vllm", _ThirdPartyAdapter(tool_name="run_shell"))

    out = extract_tool_calls_for(_FakeVLLMClient(), response=None)
    assert len(out) == 1
    assert out[0].name == "run_shell"


def test_register_adapter_conflicting_prefix_raises(monkeypatch):
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    register_adapter("vllm", _ThirdPartyAdapter())

    with pytest.raises(ValueError, match="vllm"):
        register_adapter("vllm", _ThirdPartyAdapter())  # 다른 객체 — 충돌


def test_register_adapter_same_object_is_idempotent(monkeypatch):
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    adapter = _ThirdPartyAdapter()
    register_adapter("vllm", adapter)
    register_adapter("vllm", adapter)  # 동일 객체 재등록 — 에러 없음

    assert adapters_mod._ADAPTER_REGISTRY["vllm"] is adapter


def test_register_adapter_cannot_hijack_builtin_prefix(monkeypatch):
    """openai/anthropic는 이미 레지스트리에 있으므로 재등록 시도는 충돌로 취급."""
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    with pytest.raises(ValueError, match="openai"):
        register_adapter("openai", _ThirdPartyAdapter())
```

### Step 2: 테스트 실행해 실패 확인

Run: `.venv/bin/pytest tests/test_adapters.py -k "model_adapter_protocol or register_adapter" -v`
Expected: FAIL — `ImportError: cannot import name 'ModelAdapter' from 'rein.adapters'` (그리고 `register_adapter`도 동일)

### Step 3: `ModelAdapter` Protocol 구현

`src/rein/adapters/protocol.py` 전체를 아래로 교체:

```python
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
```

### Step 4: 레지스트리 + `register_adapter` 구현

`src/rein/adapters/__init__.py`를 아래로 교체(전체 파일):

```python
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
    """
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
```

`src/rein/adapters/builtin.py`는 수정하지 않는다 — `_builtin_kind()`가 `builtin.py`의 함수를 거치지 않고 `BUILTIN_OPENAI_PREFIX`/`BUILTIN_ANTHROPIC_PREFIX` 상수만 재사용하므로, 위 Step 4의 import 블록에는 `is_builtin_model_client`를 포함하지 않는다(미사용 import를 남기면 `ruff` F401에 걸린다). `builtin.py` 자신의 `is_builtin_model_client` 함수와 그 함수를 직접 호출하는 기존 테스트(`test_is_builtin_openai_via_module_prefix` 등, `tests/test_adapters.py:73-96`)는 그대로 둔다 — 이 함수는 더 이상 `adapters/__init__.py` 내부에서 호출되지 않지만, 여전히 `rein.adapters.builtin`에서 직접 import해 쓸 수 있는 함수로 남아 그 테스트들은 변경 없이 통과한다.

### Step 5: 새 테스트 통과 확인

Run: `.venv/bin/pytest tests/test_adapters.py -k "model_adapter_protocol or register_adapter" -v`
Expected: 7 tests PASS

### Step 6: 기존 `__all__` 하드코딩 테스트 갱신

`tests/test_adapters.py`의 `test_no_public_plugin_registration_api`(기존 271-291번 줄)를 아래로 교체:

```python
def test_public_plugin_registration_api_exposed():
    """#80: 서드파티 어댑터 등록용 공개 API가 이제 존재해야 한다.

    rein.adapters 공개 표면은 ToolUse/ModelAdapter/is_recognized_adapter/
    extract_tool_calls_for/register_adapter 5개.
    """
    import rein.adapters as adapters_mod

    assert set(adapters_mod.__all__) == {
        "ToolUse",
        "ModelAdapter",
        "is_recognized_adapter",
        "extract_tool_calls_for",
        "register_adapter",
    }
    assert callable(adapters_mod.register_adapter)
```

`test_internal_classes_not_in_public_all`(기존 403-414번 줄, 함수 내부의 `__all__` 검증)도 같은 집합으로 교체:

```python
def test_internal_classes_not_in_public_all():
    """__all__은 5개만 — providers/* 내부 클래스는 직접 노출 금지."""
    import rein.adapters as adapters_mod

    assert set(adapters_mod.__all__) == {
        "ToolUse",
        "ModelAdapter",
        "is_recognized_adapter",
        "extract_tool_calls_for",
        "register_adapter",
    }
    # OpenAIAdapter / AnthropicAdapter는 __all__에 없음.
    assert "OpenAIAdapter" not in adapters_mod.__all__
    assert "AnthropicAdapter" not in adapters_mod.__all__
```

### Step 7: 전체 어댑터 테스트 스위트 통과 확인

Run: `.venv/bin/pytest tests/test_adapters.py -v`
Expected: 모든 테스트 PASS (기존 테스트 포함, 신규 7개 포함 총 40+ 개)

### Step 8: Lint

Run: `.venv/bin/ruff check src/rein/adapters/ tests/test_adapters.py && .venv/bin/ruff format --check src/rein/adapters/ tests/test_adapters.py`
Expected: 에러 없음 (포맷 diff 있으면 `.venv/bin/ruff format src/rein/adapters/ tests/test_adapters.py` 실행 후 재확인)

### Step 9: Commit

```bash
git add src/rein/adapters/protocol.py src/rein/adapters/__init__.py tests/test_adapters.py
git commit -m "feat: register_adapter 공개 API로 서드파티 어댑터 등록 지원 (#80)"
```

---

## Task 2: 서드파티 등록 어댑터는 자동 배선되지 않음을 harness 레벨에서 검증

**Files:**
- Test: `tests/test_harness_issue_73.py` (기존 파일에 케이스 추가 — #73과 같은 관측 배선 계약을 다루므로 같은 파일이 자연스럽다)

**Interfaces:**
- Consumes: Task 1의 `rein.adapters.register_adapter`, 기존 `Harness.observe_model`(`src/rein/harness.py:253`), 기존 `harness` fixture(`tests/test_harness_issue_73.py:115-117`).
- Produces: 없음 (테스트 전용 태스크).

### Step 1: 실패하는 테스트 작성

`tests/test_harness_issue_73.py` 상단 import 블록(21-29번 줄)에 추가:

```python
from rein.adapters import register_adapter
```

파일 끝(242번 줄 이후)에 추가:

```python
def test_registered_third_party_adapter_not_auto_wired(harness, monkeypatch):
    """#80: register_adapter로 등록된 서드파티는 인식되지만 자동
    몽키패치 배선 대상이 아니다 — duck-typed 커스텀 클라이언트와
    동일하게 _observed_client만 세팅된다."""
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

    class _VLLMAdapter:
        def extract_tool_calls(self, response):
            return []

    class _FakeVLLMClient:
        pass

    _FakeVLLMClient.__module__ = "vllm.entrypoints"
    register_adapter("vllm", _VLLMAdapter())

    client = _FakeVLLMClient()
    harness.observe_model(client)

    assert harness._observed_client is client
    assert harness._patch_state is None
```

### Step 2: 테스트 실행해 통과 확인 (구현 변경 불필요)

Run: `.venv/bin/pytest tests/test_harness_issue_73.py -k test_registered_third_party_adapter_not_auto_wired -v`
Expected: PASS — Task 1에서 이미 `_patch_target_for`가 `_builtin_kind`(하드코딩 두 개)만 보도록 구현했으므로 별도 프로덕션 코드 변경 없이 통과해야 한다. FAIL한다면 Task 1의 `_patch_target_for`/`_builtin_kind` 분리가 잘못된 것이므로 Task 1 구현을 다시 확인한다.

### Step 3: 전체 harness 테스트 스위트 회귀 확인

Run: `.venv/bin/pytest tests/test_harness_issue_73.py -v`
Expected: 모든 테스트(기존 + 신규) PASS

### Step 4: Lint

Run: `.venv/bin/ruff check tests/test_harness_issue_73.py && .venv/bin/ruff format --check tests/test_harness_issue_73.py`
Expected: 에러 없음

### Step 5: Commit

```bash
git add tests/test_harness_issue_73.py
git commit -m "test: register_adapter 등록 서드파티는 자동 배선 대상 아님을 harness 레벨로 검증 (#80)"
```

---

## Task 3: CLAUDE.md §3 문서 갱신 (living file)

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1에서 확정된 `register_adapter`/`ModelAdapter` 실제 시그니처.
- Produces: 없음 (문서 전용 태스크).

### Step 1: "공개 플러그인 경로는 지금 열지 않는다" 문단 교체

`CLAUDE.md`의 102-105번 줄(정확한 텍스트):

```
이 프로토콜은 **공개 확장 포인트가 아니라 내장 어댑터의 내부 구현
디테일**이다. 서드파티가 자기 프로바이더용 어댑터를 등록하는 공개
플러그인 경로는 지금 열지 않는다 — §12 M4 "추가 어댑터" 항목에서
별도로 설계한다. 지금 열면 M1 스코프로 슬며시 들어오는 크리프가 된다.
```

를 아래로 교체:

```
이 두 갈래(내장 자동 감지 / 최소 프로토콜)는 내장 어댑터의 내부 구현
디테일이었지만, **M4(#80)에서 서드파티 공개 플러그인 경로가 열렸다**:
`rein.adapters.register_adapter(module_prefix: str, adapter:
ModelAdapter) -> None`으로 자기 프로바이더 prefix를 등록하면, 사용자는
원시 클라이언트를 그대로 `observe_model(client)`에 넘기는 것만으로
내장(openai/anthropic)과 동일한 경로로 인식·라우팅된다.

```python
# 서드파티 패키지 내부
from rein.adapters import register_adapter, ModelAdapter

class VLLMAdapter(ModelAdapter):
    def extract_tool_calls(self, response): ...

register_adapter("vllm", VLLMAdapter())
```

같은 `module_prefix`에 '다른' 어댑터가 이미 등록돼 있으면
`register_adapter()`가 즉시 `ValueError`(fail-closed) — 두 서드파티
패키지의 prefix 충돌을 조용히 덮어쓰지 않는다. 동일 객체(identity)
재등록은 idempotent하게 허용한다.

**자동 몽키패치 배선(아래 "자동 배선 범위" 절)은 여전히
내장(OpenAI/Anthropic) 전용이다.** `register_adapter`로 등록된
서드파티 클라이언트는 인식·라우팅만 되고, 호출 메서드 자동 가로채기
대상은 아니다 — 호출 진입점(메서드명·시그니처·동기/스트리밍 여부)에
대한 계약이 없는 임의 객체의 속성을 자동으로 덮어쓰는 것은 안전하지
않기 때문이다(§3 자동 배선 범위 절과 동일 근거). 이 경우 사용자가
`harness._observe(response)`를 직접 호출해야 한다.

entry_points/setuptools 기반 자동 탐색(설치만 해도 import 없이
인식되는 방식)은 채택하지 않았다 — 사용자가 서드파티 패키지를
명시적으로 `import`해야 등록 side effect가 실행되는 편이 "숨은 마법
없음" 원칙(§5 fail-closed)과 일관된다.

vLLM/Ollama 등 구체적인 로컬 런타임용 어댑터 구현 자체는 이 공개
프로토콜과 별개다 — `providers/local.py`는 여전히 스켈레톤이며,
로컬 클라이언트를 내장 자동 감지(module prefix) 목록에 추가할지는
별도 결정 사항이다(§3 "로컬 클라이언트" TODO는 아래 그대로 유지).
```

### Step 2: 문서 렌더링 확인 (오탈자/링크 확인, 자동화된 검증 없음)

문서 파일이므로 자동 테스트는 없다. 대신 아래로 diff를 눈으로 확인한다.

Run: `git diff CLAUDE.md`
Expected: 102-105번 줄 영역만 바뀌고, 그 위(82-100번 줄, 두 갈래 설명)와 아래(107번 줄부터 "자동 배선 범위" 절)는 그대로.

### Step 3: Commit

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md §3에 register_adapter 공개 플러그인 경로 반영 (#80)"
```

---

## Self-Review Notes (완료 후 확인용)

- **스펙 커버리지**: 스펙 §1(레지스트리 통합) → Task 1. §2(공개 API + 사용 예 + entry_points 미채택) → Task 1 구현 + Task 3 문서. §3(문서 갱신) → Task 3. §4(테스트: 등록/충돌/idempotent/미배선) → Task 1(등록·충돌·idempotent) + Task 2(미배선). 스코프 밖 항목(vLLM 구현·entry_points·자동배선·unregister)은 어떤 태스크에도 포함하지 않았음을 Task 3 Step 1 마지막 문단과 Global Constraints에서 명시.
- **타입 일관성**: `ModelAdapter`(protocol.py) → `register_adapter(module_prefix: str, adapter: ModelAdapter)`(`__init__.py`) → CLAUDE.md 예제 전부 동일 시그니처.
- **회귀 위험**: `is_builtin_model_client`(`builtin.py`)는 코드·테스트 모두 미변경 — Task 1은 `adapters/__init__.py`에서 그 함수의 import만 제거(미사용이 되므로)하고 `builtin.py` 자체는 건드리지 않는다.

"""어댑터 구현 검증 (CLAUDE.md §3).

검증 범위:
    - 내장 3개 프로바이더 타입 자동 감지 (모듈 prefix)
    - extract_tool_calls 최소 프로토콜 동작 (OpenAI / Anthropic)
    - 로컬 어댑터 자동 감지 미지원 명시 (TODO 보존)
    - is_recognized_adapter: 둘 중 하나 만족 / 둘 다 불만족
    - 공개 확장 플러그인 경로 미노출 (M4 이연)
"""

from __future__ import annotations

import pytest

from rein.adapters import (
    ModelAdapter,
    ToolUse,
    extract_tool_calls_for,
    is_recognized_adapter,
    register_adapter,
)
from rein.adapters.builtin import is_builtin_model_client
from rein.adapters.protocol import has_extract_tool_calls
from rein.adapters.providers.anthropic import AnthropicAdapter
from rein.adapters.providers.local import LocalAdapter
from rein.adapters.providers.openai import OpenAIAdapter

# ---- 가짜 SDK 응답 객체 ----


class _FakeOpenAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeOpenAIChoice:
    def __init__(self, message):
        self.message = message


class _FakeOpenAIResponse:
    def __init__(self, choices):
        self.choices = choices


class _FakeOpenAIClient:
    pass


class _FakeAnthropicBlock:
    def __init__(self, type_, name=None, input_=None):
        self.type = type_
        self.name = name
        self.input = input_


class _FakeAnthropicResponse:
    def __init__(self, content):
        self.content = content


class _FakeAnthropicClient:
    pass


class _FakeLocalClient:
    pass


# openai / anthropic 모듈의 __name__은 실제론 'openai'/'anthropic'이지만,
# 테스트 격리를 위해 클래스의 __module__을 강제로 패치한다.
def _patch_module(cls, module_name: str) -> None:
    cls.__module__ = module_name


# ---- 내장 타입 자동 감지 ----


def test_is_builtin_openai_via_module_prefix():
    _patch_module(_FakeOpenAIClient, "openai.resources.chat")
    assert is_builtin_model_client(_FakeOpenAIClient()) is True


def test_is_builtin_anthropic_via_module_prefix():
    _patch_module(_FakeAnthropicClient, "anthropic.resources.messages")
    assert is_builtin_model_client(_FakeAnthropicClient()) is True


def test_local_client_not_auto_detected():
    """§3 TODO 보존: 로컬 클라이언트는 모듈 prefix 자동 감지에서 빠진다.

    그 전까지 로컬은 §3 두 번째 갈래(extract_tool_calls 구현)로만
    인식된다.
    """
    _patch_module(_FakeLocalClient, "my_local_runtime.client")
    assert is_builtin_model_client(_FakeLocalClient()) is False


def test_unknown_client_not_builtin():
    _patch_module(_FakeLocalClient, "requests")
    assert is_builtin_model_client(_FakeLocalClient()) is False


# ---- 최소 프로토콜: has_extract_tool_calls ----


def test_has_extract_tool_calls_true():
    a = OpenAIAdapter()

    class _OnlyMethod:
        def extract_tool_calls(self, r):
            return []

    assert has_extract_tool_calls(a) is True
    assert has_extract_tool_calls(_OnlyMethod()) is True


def test_has_extract_tool_calls_false():
    class _NoMethod:
        pass

    assert has_extract_tool_calls(_NoMethod()) is False
    assert has_extract_tool_calls(None) is False
    assert has_extract_tool_calls("not an object") is False


# ---- is_recognized_adapter: 두 갈래 통합 ----


def test_recognized_via_builtin_module_prefix():
    _patch_module(_FakeOpenAIClient, "openai")
    assert is_recognized_adapter(_FakeOpenAIClient()) is True


def test_recognized_via_protocol_duck_typing():
    """내장 모듈 prefix가 아니더라도 extract_tool_calls 구현이면 인정."""

    class _ThirdParty:
        def extract_tool_calls(self, response):
            return [ToolUse(name="x", args={})]

    _patch_module(_ThirdParty, "my_sdk.client")
    assert is_recognized_adapter(_ThirdParty()) is True


def test_not_recognized_when_neither():
    _patch_module(_FakeLocalClient, "unrelated.module")

    class _Plain:
        pass

    _patch_module(_Plain, "another.module")
    assert is_recognized_adapter(_FakeLocalClient()) is False
    assert is_recognized_adapter(_Plain()) is False
    assert is_recognized_adapter(None) is False


# ---- OpenAIAdapter.extract_tool_calls ----


def test_openai_extracts_tool_calls_from_sdk_object():
    response = _FakeOpenAIResponse(
        choices=[
            _FakeOpenAIChoice(
                message=_FakeOpenAIMessage(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "execute_sql",
                                "arguments": '{"query": "SELECT 1"}',
                            },
                        }
                    ]
                )
            )
        ]
    )

    out = OpenAIAdapter().extract_tool_calls(response)
    assert len(out) == 1
    assert out[0].name == "execute_sql"
    assert out[0].args == {"query": "SELECT 1"}


def test_openai_handles_dict_arguments():
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "noop",
                                "arguments": {"k": "v"},  # dict로 올 수도 있음
                            }
                        }
                    ]
                }
            }
        ]
    }
    out = OpenAIAdapter().extract_tool_calls(response)
    assert out[0].args == {"k": "v"}


def test_openai_skips_malformed_arguments():
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "broken", "arguments": "not-json{"}},
                    ]
                }
            }
        ]
    }
    out = OpenAIAdapter().extract_tool_calls(response)
    assert len(out) == 1
    assert out[0].name == "broken"
    assert out[0].args == {}  # 파싱 실패 시 빈 dict


def test_openai_empty_when_no_tool_calls():
    response = {"choices": [{"message": {"tool_calls": []}}]}
    assert OpenAIAdapter().extract_tool_calls(response) == []


# ---- AnthropicAdapter.extract_tool_calls ----


def test_anthropic_extracts_tool_use_blocks():
    response = _FakeAnthropicResponse(
        content=[
            _FakeAnthropicBlock(type_="text", name=None, input_=None),
            _FakeAnthropicBlock(type_="tool_use", name="delete_file", input_={"path": "/tmp/x"}),
        ]
    )
    out = AnthropicAdapter().extract_tool_calls(response)
    assert len(out) == 1
    assert out[0].name == "delete_file"
    assert out[0].args == {"path": "/tmp/x"}


def test_anthropic_skips_text_blocks():
    response = {
        "content": [{"type": "text", "text": "hello"}],
    }
    assert AnthropicAdapter().extract_tool_calls(response) == []


def test_anthropic_handles_dict_response():
    response = {
        "content": [
            {"type": "tool_use", "name": "f", "input": {"a": 1}},
        ]
    }
    out = AnthropicAdapter().extract_tool_calls(response)
    assert out[0].name == "f"
    assert out[0].args == {"a": 1}


# ---- LocalAdapter: §3 TODO 보존 ----


def test_local_skeleton_returns_empty():
    """§3 TODO: 로컬 응답 포맷은 M4. 스켈레톤은 보수적으로 빈 리스트."""
    assert LocalAdapter().extract_tool_calls({"anything": True}) == []
    assert LocalAdapter().extract_tool_calls(None) == []


# ---- 공개 확장 플러그인 경로 미노출 ----


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


# ---- extract_tool_calls_for 위임 헬퍼 ----


def test_delegate_routes_openai_builtin_to_openai_adapter():
    """모듈 prefix만 있고 메서드 없는 순정 OpenAI 인스턴스도 라우팅으로 추출."""

    class _PlainOpenAIClient:
        # 의도적으로 extract_tool_calls 없음 — 순정 SDK 인스턴스 모델링.
        pass

    _patch_module(_PlainOpenAIClient, "openai.resources.chat")

    fake_response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "execute_sql",
                                "arguments": '{"query": "SELECT 1"}',
                            }
                        }
                    ]
                }
            }
        ]
    }

    out = extract_tool_calls_for(_PlainOpenAIClient(), fake_response)
    assert len(out) == 1
    assert out[0].name == "execute_sql"
    assert out[0].args == {"query": "SELECT 1"}


def test_delegate_routes_anthropic_builtin_to_anthropic_adapter():
    class _PlainAnthropicClient:
        pass

    _patch_module(_PlainAnthropicClient, "anthropic.resources.messages")

    fake_response = {
        "content": [{"type": "tool_use", "name": "delete_file", "input": {"path": "/x"}}]
    }

    out = extract_tool_calls_for(_PlainAnthropicClient(), fake_response)
    assert len(out) == 1
    assert out[0].name == "delete_file"
    assert out[0].args == {"path": "/x"}


def test_delegate_falls_back_to_client_own_method():
    """내장 자동 감지 미해당 시 client 자신의 extract_tool_calls 호출."""

    class _CustomAdapter:
        def extract_tool_calls(self, response):
            return [ToolUse(name="custom_tool", args={"k": 1})]

    _patch_module(_CustomAdapter, "my_local_runtime.client")

    out = extract_tool_calls_for(_CustomAdapter(), response=None)
    assert len(out) == 1
    assert out[0].name == "custom_tool"


def test_delegate_raises_on_unrecognized_client():
    """is_recognized_adapter가 False인 client는 위임 헬퍼도 즉시 실패."""

    class _Unknown:
        pass

    _patch_module(_Unknown, "requests")

    with pytest.raises(TypeError, match="인식된 어댑터가 아닙니다"):
        extract_tool_calls_for(_Unknown(), response=None)


def test_callable_check_in_duck_typing():
    """§3 두 번째 갈래는 '속성 존재'만이 아니라 '호출 가능'까지 본다."""

    class _NonCallable:
        extract_tool_calls = "not a method"  # 속성은 있지만 callable 아님

    _patch_module(_NonCallable, "my_sdk.client")
    assert is_recognized_adapter(_NonCallable()) is False
    assert has_extract_tool_calls(_NonCallable()) is False


def test_extract_tool_calls_for_uses_singleton_adapters():
    """모듈 레벨 싱글톤 사용 — 호출마다 인스턴스 재생성 안 함."""
    from rein.adapters import _ANTHROPIC_ADAPTER, _OPENAI_ADAPTER

    class _PlainOpenAI:
        pass

    _patch_module(_PlainOpenAI, "openai")

    # 두 번 호출해도 동일 인스턴스 응답을 사용하는지 직접 확인.
    fake = {
        "choices": [{"message": {"tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}}]
    }
    out1 = extract_tool_calls_for(_PlainOpenAI(), fake)
    out2 = extract_tool_calls_for(_PlainOpenAI(), fake)
    assert out1 == out2
    # 싱글톤 인스턴스 자체가 살아있어야 함.
    assert _OPENAI_ADAPTER is not None
    assert _ANTHROPIC_ADAPTER is not None


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


# ---- register_adapter: 서드파티 등록 (#80) ----


class _ThirdPartyAdapter:
    def __init__(self, tool_name: str = "third_party_tool"):
        self._tool_name = tool_name

    def extract_tool_calls(self, response):
        return [ToolUse(name=self._tool_name, args={})]


def test_model_adapter_protocol_is_structural():
    """ModelAdapter는 명시적 상속 없이도 구조적으로 만족되는 Protocol이어야 한다."""
    assert isinstance(_ThirdPartyAdapter(), ModelAdapter)


def test_register_adapter_makes_prefix_recognized():
    class _FakeVLLMClient:
        pass

    _patch_module(_FakeVLLMClient, "vllm.entrypoints")

    adapter = _ThirdPartyAdapter()
    register_adapter("vllm", adapter)

    assert is_recognized_adapter(_FakeVLLMClient()) is True


def test_register_adapter_routes_extract_tool_calls_for():
    class _FakeVLLMClient:
        pass

    _patch_module(_FakeVLLMClient, "vllm.entrypoints")

    register_adapter("vllm", _ThirdPartyAdapter(tool_name="run_shell"))

    out = extract_tool_calls_for(_FakeVLLMClient(), response=None)
    assert len(out) == 1
    assert out[0].name == "run_shell"


def test_register_adapter_conflicting_prefix_raises():
    register_adapter("vllm", _ThirdPartyAdapter())

    with pytest.raises(ValueError, match="vllm"):
        register_adapter("vllm", _ThirdPartyAdapter())  # 다른 객체 — 충돌


def test_register_adapter_same_object_is_idempotent():
    import rein.adapters as adapters_mod

    adapter = _ThirdPartyAdapter()
    register_adapter("vllm", adapter)
    register_adapter("vllm", adapter)  # 동일 객체 재등록 — 에러 없음

    assert adapters_mod._ADAPTER_REGISTRY["vllm"] is adapter


def test_register_adapter_cannot_hijack_builtin_prefix():
    """openai/anthropic는 이미 레지스트리에 있으므로 재등록 시도는 충돌로 취급."""
    with pytest.raises(ValueError, match="openai"):
        register_adapter("openai", _ThirdPartyAdapter())


def test_register_adapter_rejects_object_without_extract_tool_calls():
    """리뷰 finding 1 (#80): adapter가 extract_tool_calls를 구현하지
    않으면 등록 시점에 즉시 TypeError — §3 fail-closed. 이 검증이 없으면
    실패가 한참 뒤 _observe 안에서 AttributeError로만 드러나(fail-open
    관측 표면 특성상 그마저도 삼켜져) 조용한 무관측(§5 금지 패턴)이
    된다."""
    with pytest.raises(TypeError, match="extract_tool_calls"):
        register_adapter("vllm", object())


def test_register_adapter_rejects_dotted_module_prefix():
    """리뷰 finding 2 (#80): module_prefix 매칭은
    `type(client).__module__.split(".")[0]` 최상위 토큰 하나만 본다.
    "vllm.entrypoints"처럼 점이 포함된 값을 그대로 받아주면 등록은
    성공하지만 어떤 클라이언트와도 영영 매칭되지 않아 조용히
    무관측으로 빠진다."""
    with pytest.raises(ValueError, match="module_prefix"):
        register_adapter("vllm.entrypoints", _ThirdPartyAdapter())


def test_register_adapter_rejects_empty_module_prefix():
    """빈 문자열 module_prefix는 `type(client).__module__`이 falsy인
    임의 클라이언트에 우연히 매칭될 수 있어 거부한다."""
    with pytest.raises(ValueError, match="module_prefix"):
        register_adapter("", _ThirdPartyAdapter())

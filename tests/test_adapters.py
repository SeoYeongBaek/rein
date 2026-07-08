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

from rein.adapters import ToolUse, extract_tool_calls_for, is_recognized_adapter
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


def test_no_public_plugin_registration_api():
    """§12 M4 이연: 서드파티 어댑터 등록용 공개 API가 아직 없어야 한다.

    rein.adapters 공개 표면은 ToolUse + is_recognized_adapter만.
    register_adapter 류의 공개 플러그인 진입점은 §4 '이후 시그니처를
    바꾸지 않는다' 보존을 위해 지금 열지 않는다.
    """
    import rein.adapters as adapters_mod

    public_names = {n for n in dir(adapters_mod) if not n.startswith("_")}
    # __all__에 명시된 것만 공개.
    # __all__에 명시된 것만 공개. (라우터 함수 추가 반영)
    assert set(adapters_mod.__all__) == {
        "ToolUse",
        "is_recognized_adapter",
        "extract_tool_calls_for",
    }
    # register_*, plugin 같은 이름이 새지 않았는지.
    assert not any(
        name.startswith("register_") or "plugin" in name.lower() for name in public_names
    )


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

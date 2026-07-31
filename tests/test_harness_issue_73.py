"""observe_model 관측 파이프라인 배선 검증 (#73).

검증 범위:
    - 빌트인(OpenAI) 클라이언트: observe_model이 실제 호출 메서드
      (chat.completions.create)를 몽키패치해 응답을 자동으로
      _observe로 흘려보낸다.
    - 원본 응답은 그대로 반환된다(투명성 — wrapper가 응답을 바꾸지
      않음).
    - model_client 이벤트가 정확한 parent_seq(다음 tool_wrap의 seq,
      EventStore.peek_next_seq() 예측값)로 기록된다.
    - 같은 클라이언트에 observe_model을 두 번 호출해도 이중 래핑되지
      않는다(__rein_observed__ 마커).
    - Harness.__exit__ 이후 원본 메서드로 복구된다.
    - duck-typed 커스텀 클라이언트는 호출 진입점을 알 수 없어 자동
      배선 대상이 아니다.
    - 관측(_observe) 도중 예외가 나도 wrapper는 fail-open — 원래
      응답을 그대로 반환한다(§3: 관측 표면은 기록 전용, 실행을 막지
      않음).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein.adapters import ToolUse, register_adapter
from rein.harness import Harness

# ---- 가짜 OpenAI SDK 모양 클라이언트 ----


class _FakeOpenAIFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeOpenAIToolCall:
    def __init__(self, name, arguments):
        self.function = _FakeOpenAIFunction(name, arguments)


class _FakeOpenAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeOpenAIChoice:
    def __init__(self, message):
        self.message = message


class _FakeOpenAIResponse:
    def __init__(self, choices):
        self.choices = choices


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self._response


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self, response):
        self.chat = _FakeChat(_FakeCompletions(response))


# openai 모듈의 실제 __name__은 'openai'지만, 테스트 격리를 위해
# 클래스의 __module__을 강제로 패치한다(test_adapters.py와 동일 컨벤션).
_FakeOpenAIClient.__module__ = "openai.resources.chat"


def _make_openai_client(
    tool_name: str = "execute_sql", tool_args_json: str = '{"query": "SELECT 1"}'
) -> tuple[_FakeOpenAIClient, _FakeOpenAIResponse]:
    tool_call = _FakeOpenAIToolCall(tool_name, tool_args_json)
    message = _FakeOpenAIMessage([tool_call])
    choice = _FakeOpenAIChoice(message)
    response = _FakeOpenAIResponse([choice])
    return _FakeOpenAIClient(response), response


# ---- 가짜 duck-typed 커스텀 클라이언트 (자동 배선 대상 아님) ----


class _FakeCustomClient:
    def extract_tool_calls(self, response):
        return []


# ---- Harness 픽스처 (test_harness_issue_4.py와 동일 컨벤션) ----


@pytest.fixture
def mock_stage_loaders():
    with (
        patch("rein.harness.load_stage_order", return_value=["schema"]),
        patch("rein.harness.resolve_stage_order", return_value=["schema"]),
    ):
        yield


@pytest.fixture
def harness(mock_stage_loaders, tmp_path):
    return Harness(record=tmp_path / "run.jsonl")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---- 테스트 ----


def test_observe_model_patches_openai_create(harness):
    client, _ = _make_openai_client()
    original_create = client.chat.completions.create

    harness.observe_model(client)

    assert client.chat.completions.create is not original_create
    assert harness._patch_state is not None


def test_wrapped_create_returns_original_response(harness):
    client, response = _make_openai_client()
    harness.observe_model(client)

    result = client.chat.completions.create(model="gpt-4.1-nano", messages=[])

    assert result is response


def test_observe_records_model_client_event_with_correct_tool_use(harness):
    client, _ = _make_openai_client(
        tool_name="execute_sql", tool_args_json='{"query": "DROP TABLE users;"}'
    )
    harness.observe_model(client)

    client.chat.completions.create(model="gpt-4.1-nano", messages=[])
    harness._event_store.close()

    events = _read_jsonl(harness.record_path)
    model_events = [e for e in events if e["source"] == "model_client"]
    assert len(model_events) == 1
    assert model_events[0]["tool_name"] == "execute_sql"
    assert model_events[0]["args"] == {"query": "DROP TABLE users;"}
    assert model_events[0]["seq"] is None  # §9: model_client seq는 항상 null


def test_model_client_parent_seq_matches_next_tool_wrap_seq(harness):
    """#73 핵심 계약: parent_seq(관측 시점 예측값)가 실제 다음
    tool_wrap의 seq와 일치해야 한다(일반적 순차 에이전트 루프 가정)."""
    client, _ = _make_openai_client()
    harness.observe_model(client)

    @harness.register_tool
    def execute_sql(query: str) -> dict:
        return {"status": "ok"}

    client.chat.completions.create(model="gpt-4.1-nano", messages=[])
    execute_sql(query="SELECT 1")
    harness._event_store.close()

    events = _read_jsonl(harness.record_path)
    model_event = next(e for e in events if e["source"] == "model_client")
    tool_wrap_event = next(e for e in events if e["source"] == "tool_wrap")
    assert model_event["parent_seq"] == tool_wrap_event["seq"]


def test_observe_model_twice_same_client_does_not_double_wrap(harness):
    client, _ = _make_openai_client()
    harness.observe_model(client)
    once_wrapped = client.chat.completions.create

    harness.observe_model(client)

    assert client.chat.completions.create is once_wrapped


def test_exit_restores_original_method(harness):
    """복구된 메서드가 원본과 '동일한 객체'인지는 보장하지 않는다 —
    파이썬 바운드 메서드는 속성 접근마다 새 wrapper 객체를 만들기
    때문(`obj.method is obj.method`는 일반적으로 False)이다. 대신
    (1) __rein_observed__ 마커가 사라졌는지, (2) 호출해도 더 이상
    관측 이벤트가 기록되지 않는지로 "원상복구"를 검증한다.
    """
    client, _ = _make_openai_client()

    with harness:
        harness.observe_model(client)
        assert getattr(client.chat.completions.create, "__rein_observed__", False)

    assert not getattr(client.chat.completions.create, "__rein_observed__", False)

    calls_before = client.chat.completions.calls
    client.chat.completions.create(model="gpt-4.1-nano", messages=[])
    assert client.chat.completions.calls == calls_before + 1  # 원본 그대로 호출됨
    # 이 호출로 아무 기록도 남지 않아야 한다(관측 해제 확인) —
    # record_path는 lazy open이라 지금까지 아무 record_*도 없었으면
    # 파일 자체가 아직 생기지 않았을 수 있다.
    assert not harness.record_path.exists() or not any(
        e["source"] == "model_client" for e in _read_jsonl(harness.record_path)
    )


def test_duck_typed_custom_client_not_patched(harness):
    client = _FakeCustomClient()

    harness.observe_model(client)

    assert harness._observed_client is client
    assert harness._patch_state is None


def test_observe_failure_is_fail_open(harness, monkeypatch):
    client, response = _make_openai_client()
    harness.observe_model(client)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(harness, "_observe", _boom)

    with pytest.warns(UserWarning, match="응답 관측 실패"):
        result = client.chat.completions.create(model="gpt-4.1-nano", messages=[])

    assert result is response  # 관측이 터져도 원래 응답은 정상 반환


def test_registered_third_party_adapter_not_auto_wired(harness):
    """#80: register_adapter로 등록된 서드파티는 인식되지만 자동
    몽키패치 배선 대상이 아니다 — duck-typed 커스텀 클라이언트와
    동일하게 _observed_client만 세팅된다."""

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


def test_registered_third_party_adapter_observed_end_to_end(harness):
    """리뷰 finding 4 (#80): register_adapter -> observe_model ->
    _observe 직접 호출까지 실제로 이어붙였을 때, 서드파티 어댑터가
    추출한 tool_use가 JSONL의 model_client 줄로 정말 남는지 검증한다.
    단위 테스트(test_adapters.py)는 extract_tool_calls_for 라우팅만,
    test_registered_third_party_adapter_not_auto_wired는 자동 배선이
    '안' 되는 것만 확인해, 사용자가 실제로 밟게 될 조합 흐름(등록 ->
    observe_model -> harness._observe(response) 수동 호출 -> 로그 확인)
    은 이전까지 아무 테스트도 커버하지 않았다."""

    class _VLLMAdapter:
        def extract_tool_calls(self, response):
            return [ToolUse(name="run_shell", args={"cmd": "ls"})]

    class _FakeVLLMClient:
        pass

    _FakeVLLMClient.__module__ = "vllm.entrypoints"
    register_adapter("vllm", _VLLMAdapter())

    client = _FakeVLLMClient()
    harness.observe_model(client)

    # 서드파티 클라이언트는 자동 배선 대상이 아니므로(§3), 사용자가
    # 자기 호출부에서 harness._observe(response)를 직접 호출해야 한다.
    harness._observe({"anything": "raw response shape은 어댑터가 해석"})
    harness._event_store.close()

    events = _read_jsonl(harness.record_path)
    model_events = [e for e in events if e["source"] == "model_client"]
    assert len(model_events) == 1
    assert model_events[0]["tool_name"] == "run_shell"
    assert model_events[0]["args"] == {"cmd": "ls"}

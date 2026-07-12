"""이슈 #15 검증: `@h.register_tool`이 (a) 순수 Python 루프와 (b) 외부
프레임워크(LangChain)에 실제로 붙여도 충돌 없이 동작하는지 확인한다.

CLAUDE.md §12 M1 완료 조건: 이 검증 전까지 "5줄 통합" 문구를 데모/문서에
쓰지 않는다. 이 파일이 그 검증의 실물 증거다.

(b) 검증 과정에서 실제 충돌을 하나 발견했다: `register_tool`의 wrapper가
`functools.wraps`를 쓰지 않아 `__name__`/`__doc__`/시그니처가 소실됐고,
LangChain의 `@tool`은 이 메타데이터로 스키마를 추론하므로
`@tool` 위에 `@h.register_tool`을 올리면 즉시
`ValueError: Function must have a docstring`로 죽었다. harness.py의
wrapper에 `functools.wraps(func)`를 추가해 수정했다(register_tool의
동작 자체는 바뀌지 않음 — 메타데이터 보존만 추가).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein import Denied
from rein.guardrails.verdict import Verdict
from rein.harness import Harness

langchain_core = pytest.importorskip("langchain_core")
from langchain_core.tools import tool as lc_tool  # noqa: E402


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def harness_factory(tmp_path):
    """스테이지 로더를 모킹해 파일 I/O 없이 기본 4단계로 Harness를 만든다."""

    def _make(record_name: str = "run.jsonl") -> Harness:
        with (
            patch(
                "rein.harness.load_stage_order",
                return_value=["schema", "permission", "budget", "safety"],
            ),
            patch(
                "rein.harness.resolve_stage_order",
                return_value=["schema", "permission", "budget", "safety"],
            ),
        ):
            return Harness(record=tmp_path / record_name)

    return _make


# --- (a) 순수 Python 루프 ---


def test_register_tool_in_pure_python_agent_loop(harness_factory):
    """프레임워크 없이 직접 짠 최소 에이전트 루프에서 register_tool이 충돌 없이 동작."""
    h = harness_factory()

    @h.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    # "에이전트 루프": LLM 대신 고정된 계획을 순서대로 실행하는 최소 루프.
    plan = [{"tool": "add", "args": {"a": 2, "b": 3}}, {"tool": "add", "args": {"a": 10, "b": -1}}]
    tools = {"add": add}

    results = [tools[step["tool"]](**step["args"]) for step in plan]

    assert results == [5, 9]
    events = _read_events(h.record_path)
    assert [e["source"] for e in events] == ["tool_wrap", "outcome", "tool_wrap", "outcome"]
    assert all(e["outcome"]["status"] == "ok" for e in events if e["source"] == "outcome")


def test_register_tool_denial_stops_pure_python_loop(harness_factory):
    """가드레일이 deny하면 순수 Python 루프에서도 원본 함수가 호출되지 않고 예외로 멈춘다."""
    h = harness_factory()
    h.register_stage(
        "safety", lambda tool_call, ctx: (Verdict.DENY, "rule_test", "테스트 차단", "evt_test")
    )

    calls = []

    @h.register_tool
    def delete_file(path: str) -> None:
        calls.append(path)

    with pytest.raises(Denied):
        delete_file(path="/tmp/whatever")

    assert calls == []  # 원본 함수는 실행되지 않았다


# --- (b) 외부 프레임워크(LangChain) ---


def test_register_tool_composes_with_langchain_tool_decorator(harness_factory):
    """`@h.register_tool`로 감싼 함수를 LangChain의 `@tool`로 다시 감싸도
    스키마 추론(name/description/args)과 실행이 정상 동작한다.

    발견한 충돌: functools.wraps 누락 시 LangChain이 docstring/시그니처를
    못 읽어 데코레이션 시점에 즉시 ValueError. harness.py 수정으로 해결.
    """
    h = harness_factory()

    @lc_tool
    @h.register_tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    assert add.name == "add"
    assert add.description == "Add two integers."
    assert set(add.args.keys()) == {"a", "b"}

    result = add.invoke({"a": 2, "b": 3})
    assert result == 5

    events = _read_events(h.record_path)
    assert events[0]["source"] == "tool_wrap"
    assert events[0]["tool_name"] == "add"
    assert events[1]["source"] == "outcome"
    assert events[1]["outcome"]["status"] == "ok"


def test_register_tool_denial_propagates_through_langchain_invoke(harness_factory):
    """LangChain 프레임워크의 `.invoke()` 경유 호출에서도 deny 판정이
    조용히 삼켜지지 않고 rein의 `Denied` 예외로 그대로 전파된다.

    (LangChain 도구는 기본적으로 예외를 삼키지 않고 그대로 올린다 —
    `handle_tool_error`를 명시적으로 켜야만 삼켜지므로, 여기서 기본
    동작이 rein의 fail-closed 계약과 충돌하지 않음을 확인한다.)
    """
    h = harness_factory()
    h.register_stage(
        "safety", lambda tool_call, ctx: (Verdict.DENY, "rule_test", "테스트 차단", "evt_test")
    )

    @lc_tool
    @h.register_tool
    def danger(x: int) -> int:
        """A dangerous operation."""
        return x

    tool_call = {"name": "danger", "args": {"x": 1}, "id": "call_1", "type": "tool_call"}
    with pytest.raises(Denied):
        danger.invoke(tool_call)

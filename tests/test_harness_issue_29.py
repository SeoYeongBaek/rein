"""Harness ↔ EventStore 배선 테스트 (이슈 #29, CLAUDE.md §6/§9).

Harness._intercept가 실제로 EventStore를 통해 tool_wrap + outcome
이벤트를 JSONL 파일에 append하는지 검증한다. 이 배선 전에는
`Harness(record=...)`로 기록한 도구 호출이 로그 파일에 전혀 남지
않았다(§29).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein.harness import Harness


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def harness(tmp_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["schema"]),
        patch("rein.harness.resolve_stage_order", return_value=["schema"]),
    ):
        yield Harness(record=tmp_path / "run.jsonl")


def test_successful_call_appends_tool_wrap_and_ok_outcome(harness):
    """도구 1회 호출 성공 시 tool_wrap + outcome(status=ok) 이벤트가 append된다."""

    @harness.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3

    events = _read_events(harness.record_path)
    assert len(events) == 2

    tool_wrap, outcome = events
    assert tool_wrap["source"] == "tool_wrap"
    assert tool_wrap["tool_name"] == "add"
    assert tool_wrap["verdict"] == "allow"
    assert tool_wrap["seq"] == 0

    assert outcome["source"] == "outcome"
    assert outcome["evt"] == tool_wrap["evt"]
    assert outcome["parent_seq"] == tool_wrap["seq"]
    assert outcome["outcome"]["status"] == "ok"
    assert outcome["outcome"]["severity"] == "info"


def test_failing_call_appends_tool_wrap_and_error_outcome(harness):
    """도구 호출이 예외를 던지면 outcome(status=error)이 기록되고 예외는 그대로 전파된다."""

    @harness.register_tool
    def boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        boom()

    events = _read_events(harness.record_path)
    assert len(events) == 2

    tool_wrap, outcome = events
    assert tool_wrap["source"] == "tool_wrap"
    assert outcome["source"] == "outcome"
    assert outcome["outcome"]["status"] == "error"
    assert outcome["outcome"]["severity"] == "warning"
    assert "kaboom" in outcome["outcome"]["detail"]


def test_multiple_calls_share_monotonic_seq(harness):
    """여러 번 호출하면 tool_wrap의 seq가 1..N으로 단조 증가한다(§6 매칭 키)."""

    @harness.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    add(1, 2)
    add(3, 4)

    events = _read_events(harness.record_path)
    tool_wraps = [e for e in events if e["source"] == "tool_wrap"]
    assert [e["seq"] for e in tool_wraps] == [0, 1]

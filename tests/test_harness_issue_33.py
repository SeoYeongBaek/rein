"""StageFn 런타임 계약 테스트 (이슈 #33, CLAUDE.md §5).

`StageFn` 프로토콜의 타입힌트가 실제 반환값(4-tuple)과 일치하는지, 그리고
`register_stage`로 등록한 커스텀 스테이지가 실제로 `_intercept` 경로를
타서 (a) ALLOW면 도구가 실행되고 (b) DENY면 막히는지 확인한다.
"""

from typing import Any
from unittest.mock import patch

import pytest

from rein.guardrails import StageFn
from rein.guardrails.exceptions import Denied
from rein.guardrails.verdict import Verdict
from rein.harness import Harness


@pytest.fixture
def harness(tmp_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["custom"]),
        patch("rein.harness.resolve_stage_order", return_value=["custom"]),
    ):
        yield Harness(record=tmp_path / "run.jsonl")


def test_stage_fn_protocol_matches_runtime_4_tuple_contract():
    """StageFn 프로토콜대로 4-tuple을 반환하는 함수가 구조적으로 인정되는지 확인."""

    def stage_fn(tool_call: Any, context: Any) -> tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    assert isinstance(stage_fn, StageFn)
    verdict, rule_id, rationale, evt_id = stage_fn({}, None)
    assert verdict == Verdict.ALLOW


def test_custom_stage_allow_routes_through_intercept_and_executes_tool(harness):
    """커스텀 스테이지가 ALLOW를 반환하면 _intercept를 통과해 도구가 실제로 실행된다."""
    calls: list[tuple[Any, Any]] = []

    def custom_allow(tool_call: Any, ctx: Any) -> tuple[Verdict, str, str, str]:
        calls.append((tool_call, ctx))
        return Verdict.ALLOW, "", "", ""

    harness.register_stage("custom", custom_allow)

    @harness.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3
    assert len(calls) == 1
    assert calls[0][0]["name"] == "add"


def test_custom_stage_deny_routes_through_intercept_and_blocks_tool(harness):
    """커스텀 스테이지가 DENY를 반환하면 _intercept가 예외를 던지고 도구는 실행되지 않는다."""
    tool_executed = False

    def custom_deny(tool_call: Any, ctx: Any) -> tuple[Verdict, str, str, str]:
        return Verdict.DENY, "rule_custom", "커스텀 차단", "evt_custom"

    harness.register_stage("custom", custom_deny)

    @harness.register_tool
    def dangerous() -> str:
        nonlocal tool_executed
        tool_executed = True
        return "실행됨"

    with pytest.raises(Denied):
        dangerous()

    assert not tool_executed

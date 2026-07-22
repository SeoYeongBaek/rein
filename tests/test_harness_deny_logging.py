"""deny 판정도 이벤트 저장소에 tool_wrap으로 기록되는지 검증
(M3 스펙, harness.py 버그 픽스 B).

픽스 전에는 _intercept가 non-allow 판정에서 즉시 예외를 던지고
return하느라 record_tool_wrap을 전혀 호출하지 않아, 막힌 호출이
JSONL에 한 줄도 남지 않았다.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein.guardrails.exceptions import Denied
from rein.guardrails.verdict import Verdict
from rein.harness import Harness


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def harness(tmp_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        yield Harness(record=tmp_path / "run.jsonl")


def test_denied_call_is_recorded_as_tool_wrap_with_no_outcome(harness):
    """커스텀 safety 스테이지가 deny하면 tool_wrap 줄만 기록되고 outcome은 없다."""
    harness.register_stage(
        "safety",
        lambda tool_call, ctx: (Verdict.DENY, "rule_custom", "차단 사유", "evt_placeholder"),
    )

    @harness.register_tool
    def dangerous() -> str:
        return "실행됨"

    with pytest.raises(Denied) as exc_info:
        dangerous()

    events = _read_events(harness.record_path)
    assert len(events) == 1

    tool_wrap = events[0]
    assert tool_wrap["source"] == "tool_wrap"
    assert tool_wrap["tool_name"] == "dangerous"
    assert tool_wrap["verdict"] == "deny"

    # 스테이지가 반환한 placeholder("evt_placeholder")가 아니라 방금
    # 기록된 진짜 evt로 예외가 채워진다.
    assert exc_info.value.evt_id == tool_wrap["evt"]
    assert exc_info.value.evt_id != "evt_placeholder"


def test_allowed_call_after_denied_call_still_gets_own_tool_wrap_and_outcome(harness):
    """deny 이후에도 seq/evt 카운터가 정상적으로 이어져 다음 allow 호출이 제대로 기록된다."""
    verdicts = iter([Verdict.DENY, Verdict.ALLOW])

    def flaky_safety(tool_call, ctx):
        verdict = next(verdicts)
        if verdict == Verdict.DENY:
            return Verdict.DENY, "rule_custom", "차단 사유", ""
        return Verdict.ALLOW, "", "", ""

    harness.register_stage("safety", flaky_safety)

    @harness.register_tool
    def maybe_dangerous() -> str:
        return "실행됨"

    with pytest.raises(Denied):
        maybe_dangerous()

    assert maybe_dangerous() == "실행됨"

    events = _read_events(harness.record_path)
    assert [e["source"] for e in events] == ["tool_wrap", "tool_wrap", "outcome"]
    assert events[0]["verdict"] == "deny"
    assert events[1]["verdict"] == "allow"
    assert events[2]["outcome"]["status"] == "ok"

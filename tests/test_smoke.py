"""M1 이전 스모크 테스트. 실제 유닛 테스트는 각 모듈 구현과 함께 추가."""

import pytest

import rein
from rein.guardrails import UnknownStageError
from rein.guardrails.verdict import Verdict
from rein.harness import Harness


def test_package_importable():
    assert rein.__version__


def test_harness_is_context_manager():
    assert hasattr(rein.Harness, "__enter__")
    assert hasattr(rein.Harness, "__exit__")


def test_register_tool_accepts_async():
    import asyncio

    h = Harness(record="dummy.jsonl")

    @h.register_tool
    async def async_tool():
        return 42

    assert asyncio.run(async_tool()) == 42


def test_observe_model_fails_closed_on_unrecognized_client():
    h = Harness(record="dummy.jsonl")
    with pytest.raises(TypeError):
        h.observe_model(object())


def test_guardrail_exception_hierarchy():
    for exc_cls in (rein.Denied, rein.RetryRequested, rein.ApprovalRequired):
        assert issubclass(exc_cls, rein.GuardrailVerdictError)
        err = exc_cls("deny", "rule_0001", "테스트 사유", "evt_0001")
        assert err.rule_id == "rule_0001"
        assert err.rationale == "테스트 사유"


def test_concurrent_tool_call_error_importable_from_package_root():
    """최종 리뷰 Important #1: ConcurrentToolCallError도 다른 예외들처럼
    패키지 루트에서 바로 import 가능해야 한다."""
    import rein.harness

    assert rein.ConcurrentToolCallError is rein.harness.ConcurrentToolCallError
    assert issubclass(rein.ConcurrentToolCallError, RuntimeError)


def test_default_stage_order_used_without_config(tmp_path):
    h = Harness(record="dummy.jsonl", config=str(tmp_path / "missing_rein.yaml"))
    assert h._stage_order == ["schema", "permission", "budget", "safety"]


def test_register_stage_allowed_before_activation():
    h = Harness(record="dummy.jsonl")
    h.register_stage("custom", lambda tool_call, ctx: (Verdict.ALLOW, "", "", ""))
    assert "custom" in h._custom_stages


def test_register_stage_blocked_after_activation():
    h = Harness(record="dummy.jsonl")
    with h:
        pass
    with pytest.raises(RuntimeError):
        h.register_stage("custom", lambda tool_call, ctx: (Verdict.ALLOW, "", "", ""))


def test_unknown_stage_in_stage_order_fails_closed_on_activation(tmp_path):
    config_path = tmp_path / "rein.yaml"
    config_path.write_text("stage_order: [schema, permission, budget, safety_v2]\n")
    h = Harness(record="dummy.jsonl", config=str(config_path))

    with pytest.raises(UnknownStageError, match="safety_v2"):

        @h.register_tool
        def custom_tool(x: int) -> int:
            return x

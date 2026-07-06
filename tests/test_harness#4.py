from typing import Any
from unittest.mock import patch

import pytest

from rein.guardrails.exceptions import ApprovalRequired, Denied, RetryRequested
from rein.guardrails.verdict import Verdict
from rein.harness import Harness

# --- Fixtures ---


@pytest.fixture
def mock_stage_loaders():
    """파일 I/O 없이 테스트하기 위해 환경 설정 로더를 모킹합니다."""
    with (
        patch("rein.harness.load_stage_order", return_value=["schema", "permission"]),
        patch("rein.harness.resolve_stage_order", return_value=["schema", "permission"]),
    ):
        yield


@pytest.fixture
def harness(mock_stage_loaders, tmp_path):
    """테스트용 Harness 인스턴스를 제공합니다."""
    return Harness(record=tmp_path / "test.jsonl")


# --- 1. 도구 등록 및 파이프라인 봉인(Seal) 테스트 ---


def test_register_tool_success_and_seal(harness):
    """정상적인 동기 함수가 도구로 등록되고, Harness가 봉인(seal)되는지 확인"""
    assert not harness._sealed  # 등록 전에는 seal 해제 상태

    @harness.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    assert harness._sealed is True  # 등록 후 자동 seal
    assert add(2, 3) == 5  # 정상 실행 여부


def test_register_tool_rejects_async(harness):
    """비동기(async) 함수 등록 시 명세대로 TypeError를 뱉는지 확인"""
    with pytest.raises(TypeError, match="M1은 동기 함수만 지원합니다"):

        @harness.register_tool
        async def async_tool():
            pass


def test_register_stage_after_seal_fails(harness):
    """_activate(seal) 이후에 커스텀 스테이지를 추가하려 하면 에러 발생 확인"""
    # 컨텍스트 매니저 진입으로 강제 seal
    with harness:
        with pytest.raises(RuntimeError, match="register_stage는.*이전에만 호출 가능합니다"):
            harness.register_stage("late_stage", lambda tc, ctx: (Verdict.ALLOW, "", "", ""))


# --- 2. 가드레일 판정 및 단락 평가 (Short-circuit) 테스트 ---


@patch("rein.harness.resolve_stage_order")
def test_intercept_short_circuit_on_deny(mock_resolve, harness):
    """파이프라인 진행 중 Deny 판정이 나오면 도구 실행을 막고 예외를 던지는지 확인"""

    # 항상 차단(DENY)하는 악덕 커스텀 스테이지 생성
    def block_stage(tool_call: dict[str, Any], ctx: Any) -> tuple[Verdict, str, str, str]:
        return Verdict.DENY, "rule_X", "위험한 동작 감지", "evt_123"

    harness.register_stage("block_stage", block_stage)
    mock_resolve.return_value = ["block_stage"]

    # 도구가 실제로 실행되었는지 추적하는 플래그
    tool_executed = False

    @harness.register_tool
    def dangerous_tool():
        nonlocal tool_executed
        tool_executed = True
        return "폭파 완료"

    # ❌ 도구 실행 시도 -> 가드레일에 막혀 Denied 예외가 터져야 함
    with pytest.raises(Denied) as exc_info:
        dangerous_tool()

    assert not tool_executed  # 도구 본문이 절대 실행되지 않았음을 검증 (매우 중요)

    # 에러 메시지 안에 우리가 넣은 룰 ID와 이유가 잘 찍혀서 나오는지 검증하도록 수정
    error_msg = str(exc_info.value)
    assert "rule_X" in error_msg
    assert "위험한 동작 감지" in error_msg


def test_enforce_exception_mapping():
    """내부 함수 _enforce가 Verdict 종류에 맞는 올바른 예외 클래스를 던지는지 확인"""
    from rein.harness import _enforce

    # 통과는 에러 없음
    _enforce(Verdict.ALLOW, "r", "r", "e")

    with pytest.raises(Denied):
        _enforce(Verdict.DENY, "r", "r", "e")

    with pytest.raises(RetryRequested):
        _enforce(Verdict.RETRY, "r", "r", "e")

    with pytest.raises(ApprovalRequired):
        _enforce(Verdict.APPROVE, "r", "r", "e")


# --- 3. 관측 표면 (Observe) 테스트 ---


@patch("rein.harness.is_recognized_adapter")
def test_observe_model_validation(mock_is_adapter, harness):
    """observe_model 호출 시 어댑터 검증 로직이 올바르게 동작하는지 확인"""

    # 1. 인식되지 않는 어댑터인 경우
    mock_is_adapter.return_value = False
    with pytest.raises(TypeError, match="인식된 어댑터가 아닙니다"):
        harness.observe_model("invalid_client_object")

    # 2. 올바른 어댑터인 경우
    mock_is_adapter.return_value = True
    harness.observe_model("valid_openai_client")
    assert harness._observed_client == "valid_openai_client"

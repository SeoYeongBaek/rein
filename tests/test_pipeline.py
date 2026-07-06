# pytest tests/test_pipeline.py -v
import pytest

from rein.guardrails.exceptions import ApprovalRequired, Denied
from rein.guardrails.verdict import Verdict
from rein.harness import Harness


# --- 1. 기본 실행 및 비동기 차단 테스트 ---
def test_sync_tool_allow():
    """모든 가드레일이 ALLOW일 때 도구가 정상 실행되는지 확인"""
    h = Harness(record="dummy.jsonl")

    @h.register_tool
    def dummy_tool(x, y):
        return x + y

    result = dummy_tool(3, 4)
    assert result == 7


def test_async_tool_blocked():
    """M1 명세에 따라 비동기 함수 등록 시 즉시 TypeError를 던지는지 확인"""
    h = Harness(record="dummy.jsonl")

    with pytest.raises(TypeError, match="M1은 동기 함수만 지원합니다"):

        @h.register_tool
        async def async_dummy():
            pass


# --- 2. Fail-closed (초기화 실패) 테스트 ---
def test_fail_closed_initialization():
    """
    stage_order에 등록되지 않은 스테이지가 있으면 도구 등록 시
    차단되는지 확인 (Fail-Closed)
    """
    harness = Harness(record="dummy.jsonl")

    # 고의로 미등록 스테이지 'ghost_stage' 삽입
    harness._stage_order = ["schema", "ghost_stage"]

    # PM님의 설계에 따라: 도구를 등록(@register_tool)하는 순간
    # _activate()가 실행되며 미등록 스테이지를 찾아내고 에러를 터뜨려야 함!
    with pytest.raises(ValueError):

        @harness.register_tool
        def dummy_tool():
            pass


# --- 3. Short-circuit (Fail-fast) 및 Verdict 예외 테스트 ---
def test_short_circuit_deny():
    """파이프라인 진행 중 DENY 판정이 나오면 즉시 Denied 예외를 던지는지 확인"""
    h = Harness(record="dummy.jsonl")

    # 예산(budget) 스테이지를 고의로 DENY를 반환하도록 덮어쓰기
    def mock_budget_deny(tool_call, ctx):
        return Verdict.DENY, "rule_001", "예산 초과", "evt_123"

    h.register_stage("budget", mock_budget_deny)

    @h.register_tool
    def expensive_tool():
        return "실행되면 안 됨"

    with pytest.raises(Denied) as exc_info:
        expensive_tool()

    assert exc_info.value.verdict == "deny"
    assert exc_info.value.rule_id == "rule_001"


def test_short_circuit_approve():
    """DENY가 아닌 APPROVE 판정 시에도 정확한 예외(ApprovalRequired)를 던지는지 확인"""
    h = Harness(record="dummy.jsonl")

    # 권한(permission) 스테이지를 고의로 APPROVE를 반환하도록 덮어쓰기
    def mock_permission_approve(tool_call, ctx):
        return Verdict.APPROVE, "rule_002", "관리자 승인 필요", "evt_124"

    h.register_stage("permission", mock_permission_approve)

    @h.register_tool
    def sensitive_tool():
        return "실행되면 안 됨"

    with pytest.raises(ApprovalRequired) as exc_info:
        sensitive_tool()

    assert exc_info.value.verdict == "approve"

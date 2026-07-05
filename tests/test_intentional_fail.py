#  pytest tests/test_intentional_fail.py -v
import pytest

from rein.guardrails.verdict import Verdict
from rein.harness import Harness

@pytest.mark.xfail(reason="의도적인 deny")
def test_this_will_fail_deliberately():
    """고의로 실패(FAIL)를 유발하는 테스트"""
    h = Harness(record="dummy.jsonl")
    
    # 무조건 DENY를 반환하는 가드레일 스테이지로 덮어쓰기
    def strict_safety_check(tool_call, ctx):
        return Verdict.DENY, "rule_999", "무조건 실행을 막습니다!", "evt_999"
        
    h.register_stage("safety", strict_safety_check)
    
    @h.register_tool
    def my_awesome_tool():
        return "정상 작동 완료!"
        
    # 가드레일에 막혀서 Denied 예외가 터질 수밖에 없는 상황
    result = my_awesome_tool()
    
    # 여기까지 도달할 수도 없지만, 도달하더라도 실패하도록 작성
    assert result == "정상 작동 완료!"
"""공개 예외 계층 (CLAUDE.md §4).

non-allow 판정은 조용한 차단 대신 예외로 호출자에 위임한다.
"""

from __future__ import annotations


# 나중에 가드레일이 deny 판정을 내리면 이 예외를 던지는 방식
# GuardrailVerdictError 기본 클래스 + 3개의 서브 클래스
class GuardrailVerdictError(Exception):
    def __init__(self, verdict: str, rule_id: str, rationale: str, evt_id: str) -> None:
        self.verdict = verdict
        self.rule_id = rule_id
        self.rationale = rationale
        self.evt_id = evt_id
        super().__init__(f"[{verdict}] rule={rule_id}: {rationale} (evt={evt_id})")


class Denied(GuardrailVerdictError): ...


class RetryRequested(GuardrailVerdictError): ...


class ApprovalRequired(GuardrailVerdictError): ...

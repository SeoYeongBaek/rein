"""가드레일 판정 예외 (CLAUDE.md §4 register_tool 판정 계약).

non-allow 판정(deny/retry/approve)은 원본 도구를 호출하지 않고 예외를
던진다. 조용한 차단은 fail-closed 원칙 위반이다. 이 모듈은 예외
"정의"만 담당하며, 언제 던질지(가드레일 엔진 wiring)는 별도 구현 범위다.
"""

from __future__ import annotations


class GuardrailVerdictError(Exception):
    def __init__(self, verdict: str, rule_id: str, rationale: str, evt_id: str) -> None:
        self.verdict = verdict
        self.rule_id = rule_id
        self.rationale = rationale
        self.evt_id = evt_id
        super().__init__(f"[{verdict.upper()}] {rule_id}: {rationale} (evt={evt_id})")


class Denied(GuardrailVerdictError):
    """deny 판정."""


class RetryRequested(GuardrailVerdictError):
    """retry 판정. 재시도 정책은 하네스가 구현하지 않고 호출자에 위임한다(§4)."""

class ApprovalRequired(GuardrailVerdictError):
    """approve 판정. 승인 UI/콜백은 하네스가 구현하지 않고 호출자에 위임한다(§4)."""
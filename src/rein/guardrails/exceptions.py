class GuardrailVerdictError(Exception):
    """가드레일 판정에 의해 도구 실행이 차단되었을 때 발생하는 기본 예외"""
    def __init__(self, verdict: str, rule_id: str, rationale: str, evt_id: str):
        self.verdict = verdict
        self.rule_id = rule_id
        self.rationale = rationale
        self.evt_id = evt_id
        super().__init__(f"[{verdict.upper()}] Rule {rule_id}: {rationale} (Event: {evt_id})")

class Denied(GuardrailVerdictError):
    pass

class RetryRequested(GuardrailVerdictError):
    pass

class ApprovalRequired(GuardrailVerdictError):
    pass
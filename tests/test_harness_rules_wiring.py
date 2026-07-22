"""Harness(rules=...) 라이브 집행 배선 테스트 (M3 스펙, harness.py 버그 픽스 A).

Harness.__init__이 저장만 하던 rules 인자를 _default_safety_check에
연결하기 전에는, rules.yaml에 매칭되는 deny 규칙이 있어도
register_tool로 계측된 도구가 실제로는 막히지 않았다. 이 테스트는 그
배선이 실제로 동작하는지 검증한다.
"""

import textwrap
from unittest.mock import patch

import pytest

from rein.guardrails.exceptions import Denied
from rein.harness import Harness

RULES_YAML = textwrap.dedent(
    """
    rule:
      id: rule_0007
      origin: auto
      when:
        tool: execute_sql
        features:
          class: { in: [DDL_DESTRUCTIVE] }
      scope:
        agent.role: content_editor
      then: deny
      rationale: "OWASP LLM06 Excessive Agency"
    """
)


@pytest.fixture
def rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(RULES_YAML, encoding="utf-8")
    return path


@pytest.fixture
def harness(tmp_path, rules_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        yield Harness(
            record=tmp_path / "run.jsonl",
            rules=str(rules_path),
            context={"agent_role": "content_editor"},
        )


def test_live_call_matching_deny_rule_raises_denied(harness):
    """rules.yaml에 매칭되는 DDL_DESTRUCTIVE + content_editor 호출은 라이브에서 deny된다."""

    @harness.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    with pytest.raises(Denied) as exc_info:
        execute_sql(query="DROP TABLE users;")

    assert exc_info.value.rule_id == "rule_0007"


def test_live_call_not_matching_rule_still_allowed(harness):
    """같은 rules.yaml이 있어도 SQL_SAFE 쿼리는 그대로 통과한다."""

    @harness.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    assert execute_sql(query="SELECT 1") == "executed: SELECT 1"


def test_harness_without_rules_still_allows_everything(tmp_path):
    """rules를 안 주면 기존과 동일하게 항상 allow(하위 호환)."""
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        h = Harness(record=tmp_path / "run.jsonl")

    @h.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    assert execute_sql(query="DROP TABLE users;") == "executed: DROP TABLE users;"

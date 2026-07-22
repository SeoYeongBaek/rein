"""rules.yaml로 라이브 실행 중 파괴적 SQL을 막는 예제.

Harness(rules=...)의 라이브 집행 배선(harness.py 버그 픽스 A) 덕분에,
등록한 규칙이 register_tool로 계측된 도구 호출에 실제로 적용된다.

실행:
    python examples/guardrail_rule_yaml.py
"""

from pathlib import Path

from rein import Denied, Harness

RULES_PATH = Path(__file__).parent / "rules.yaml"
RECORD_PATH = Path(__file__).parent / "run_guardrail.jsonl"

h = Harness(
    record=RECORD_PATH,
    rules=str(RULES_PATH),
    context={"agent_role": "content_editor"},
)


@h.register_tool
def execute_sql(query: str) -> str:
    return f"executed: {query}"


if __name__ == "__main__":
    print(execute_sql(query="SELECT * FROM notices"))

    try:
        execute_sql(query="DROP TABLE users;")
    except Denied as exc:
        print(f"차단됨: {exc}")

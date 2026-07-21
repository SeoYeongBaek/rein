"""#46 킬러 A/B 데모의 실패(과도한 권한 행사) 트레이스를 녹화함.

content_editor가 공지사항을 수정하는 도중, 아직 규칙이 없는 상태에서
DROP TABLE을 호출하면 guardrail 없이 그대로 실행(allow)되어 로그에
남는다. 이 run.jsonl이 `rein rule-from`의 입력이 된다(CLAUDE.md §7).
"""

from __future__ import annotations

from pathlib import Path

from rein.harness import Harness

RUN_PATH = Path(__file__).with_name("run.jsonl")

# #46 확정 시나리오: 안전한 SELECT 한 건 다음, 나노급 모델이 제안한
# 과도한 권한 행사(DROP TABLE)가 이어짐.
QUERIES = (
    "SELECT * FROM posts WHERE id = 1;",
    "DROP TABLE users;",
)


def record_failure_trace(
    output_path: Path = RUN_PATH,
) -> Path:
    """content_editor가 DROP TABLE을 유도하는 흐름을 fresh JSONL로 녹화함."""
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # EventStore가 append-only이므로 기존 실행 결과를 제거함.
    output_path.unlink(missing_ok=True)

    with Harness(
        record=output_path,
        context={
            "agent_role": "content_editor",
        },
    ) as harness:

        @harness.register_tool
        def execute_sql(
            query: str,
        ) -> dict[str, str]:
            """실제 DB 대신 성공 결과만 반환하는 데모 도구임."""
            return {
                "status": "ok",
                "query": query,
            }

        for query in QUERIES:
            execute_sql(query=query)

    return output_path


if __name__ == "__main__":
    created_path = record_failure_trace()
    print(f"실패 트레이스 녹화 완료: {created_path}")

    for line in created_path.read_text(encoding="utf-8").splitlines():
        if '"DROP TABLE' in line and '"source": "tool_wrap"' in line:
            print(f"DROP TABLE 이벤트 줄: {line}")

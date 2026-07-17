"""#46 킬러 A/B 데모의 정상 골든 트레이스를 녹화한다."""

from __future__ import annotations

from pathlib import Path

from rein.harness import Harness

GOLDEN_PATH = Path(__file__).with_name("golden_run.jsonl")

# #46 확정 시나리오:
# content_editor가 공지사항 업데이트 중 실행하는 안전한 SQL 2~3건.
SAFE_QUERIES = (
    "SELECT * FROM posts WHERE id = 1;",
    "UPDATE posts SET body = 'Updated notice' WHERE id = 1;",
    "SELECT * FROM posts WHERE id = 1;",
)


def record_golden_trace(output_path: Path = GOLDEN_PATH) -> Path:
    """안전한 공지사항 업데이트 흐름을 fresh JSONL로 녹화한다."""

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # EventStore는 append-only이므로 재실행할 때 이전 결과가 누적되지 않게 한다.
    output_path.unlink(missing_ok=True)

    with Harness(
        record=output_path,
        context={"agent_role": "content_editor"},
    ) as h:

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            """데모용 SQL 도구. 실제 DB 대신 성공 결과만 반환한다."""
            return {
                "status": "ok",
                "query": query,
            }

        for query in SAFE_QUERIES:
            execute_sql(query=query)

    return output_path


if __name__ == "__main__":
    created_path = record_golden_trace()
    print(f"골든 트레이스 녹화 완료: {created_path}")

"""#46 킬러 A/B 데모의 정상 골든 트레이스를 녹화함."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rein.harness import Harness

GOLDEN_PATH = Path(__file__).with_name("golden_run.jsonl")

# #46 확정 시나리오:
# content_editor가 공지사항을 수정하는 동안 실행하는 안전한 SQL임.
SAFE_QUERIES = (
    "SELECT * FROM posts WHERE id = 1;",
    "UPDATE posts SET body = 'Updated notice' WHERE id = 1;",
    "SELECT * FROM posts WHERE id = 1;",
)


def _normalize_zero_based_seq(path: Path) -> None:
    """Harness가 기록한 1-based seq를 replay가 기대하는 0-based로 변환함."""
    events: list[dict[str, Any]] = []

    with path.open(encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} JSONL 파싱 실패: {exc}") from exc

            events.append(event)

    integer_sequences = [event["seq"] for event in events if isinstance(event.get("seq"), int)]

    if not integer_sequences:
        return

    first_seq = min(integer_sequences)

    # 이미 0-based라면 다시 변경하지 않음.
    if first_seq == 0:
        return

    if first_seq != 1:
        raise ValueError(f"지원하지 않는 seq 시작값입니다: {first_seq}")

    for event in events:
        seq = event.get("seq")
        parent_seq = event.get("parent_seq")

        if isinstance(seq, int):
            event["seq"] = seq - 1

        if isinstance(parent_seq, int):
            event["parent_seq"] = parent_seq - 1

    with path.open("w", encoding="utf-8") as file:
        for event in events:
            file.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                )
                + "\n"
            )


def record_golden_trace(
    output_path: Path = GOLDEN_PATH,
) -> Path:
    """안전한 공지사항 업데이트 흐름을 fresh JSONL로 녹화함."""
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

        for query in SAFE_QUERIES:
            execute_sql(query=query)

    # 현재 replay 검증 기준에 맞게 seq를 0부터 시작하도록 정규화함.
    _normalize_zero_based_seq(output_path)

    return output_path


if __name__ == "__main__":
    created_path = record_golden_trace()
    print(f"골든 트레이스 녹화 완료: {created_path}")

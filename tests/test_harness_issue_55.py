"""이슈 #55: Harness 실행 context 기록 경로 검증."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from rein.harness import Harness

STAGE_ORDER = ["schema", "permission", "budget", "safety"]


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_register_tool_records_provided_context(tmp_path: Path) -> None:
    """Harness에 전달한 agent_role이 tool_wrap 이벤트에 기록된다."""

    record_path = tmp_path / "run.jsonl"

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
        Harness(
            record=record_path,
            context={"agent_role": "content_editor"},
        ) as h,
    ):

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT * FROM posts WHERE id = 1;")

    events = _read_events(record_path)
    tool_wrap = next(event for event in events if event["source"] == "tool_wrap")

    assert tool_wrap["tool_name"] == "execute_sql"
    assert tool_wrap["context"] == {"agent_role": "content_editor"}


def test_register_tool_without_context_keeps_existing_behavior(tmp_path: Path) -> None:
    """context를 생략하면 기존처럼 빈 객체가 기록된다."""

    record_path = tmp_path / "run.jsonl"

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
        Harness(record=record_path) as h,
    ):

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")

    events = _read_events(record_path)
    tool_wrap = next(event for event in events if event["source"] == "tool_wrap")

    assert tool_wrap["context"] == {}

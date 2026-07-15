"""이슈 #55: A/B 데모용 golden_run.jsonl 검증."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rein.cli import app

ROOT_DIR = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT_DIR / "demo" / "ab_demo" / "golden_run.jsonl"

runner = CliRunner()


def _read_events() -> list[dict]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_golden_trace_contains_safe_content_editor_scenario() -> None:
    """content_editor의 안전한 SELECT/UPDATE 정상 호출 3건이 들어 있다."""

    assert GOLDEN_PATH.exists()

    events = _read_events()
    tool_wrap_events = [event for event in events if event["source"] == "tool_wrap"]
    outcome_events = [event for event in events if event["source"] == "outcome"]

    assert len(tool_wrap_events) == 3
    assert len(outcome_events) == 3

    assert all(event["tool_name"] == "execute_sql" for event in tool_wrap_events)
    assert all(event["context"] == {"agent_role": "content_editor"} for event in tool_wrap_events)

    queries = [event["args"]["query"] for event in tool_wrap_events]

    select_queries = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
    update_queries = [query for query in queries if query.lstrip().upper().startswith("UPDATE")]

    assert len(select_queries) == 2
    assert len(update_queries) == 1
    assert "WHERE" in update_queries[0].upper()

    assert all("DROP TABLE" not in query.upper() for query in queries)

    assert all(event["outcome"]["status"] == "ok" for event in outcome_events)
    assert all(event["outcome"]["severity"] == "info" for event in outcome_events)


def test_golden_trace_passes_rein_seed() -> None:
    """실제 커밋된 golden_run.jsonl이 rein seed 검증을 통과한다."""

    result = runner.invoke(app, ["seed", str(GOLDEN_PATH)])

    assert result.exit_code == 0, result.output
    assert "critical 0건" in result.output
    assert "골든 트레이스로 지정됨" in result.output

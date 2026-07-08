"""`rein rule-from` CLI 테스트 (CLAUDE.md §4, 이슈 #10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from rein.cli import app

runner = CliRunner()


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _tool_wrap(
    seq: int,
    tool_name: str,
    query: str,
    verdict: str = "allow",
    severity: str = "info",
    role: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "evt": f"evt_{seq:04d}",
        "seq": seq,
        "source": "tool_wrap",
        "tool_name": tool_name,
        "args": {"query": query},
        "context": {"agent_role": role} if role is not None else {},
        "verdict": verdict,
        "outcome": {
            "status": "ok" if verdict == "allow" else "error",
            "severity": severity,
            "detail": "",
        },
    }


def _run_log_with_failure(path: Path) -> None:
    _write_jsonl(
        path,
        [
            _tool_wrap(1, "execute_sql", "SELECT * FROM posts;", role="content_editor"),
            _tool_wrap(
                2,
                "execute_sql",
                "DROP TABLE users;",
                verdict="allow",
                severity="critical",
                role="content_editor",
            ),
        ],
    )


def test_missing_event_errors(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)

    result = runner.invoke(app, ["rule-from", str(log), "--event", "evt_9999"])

    assert result.exit_code == 1
    assert "evt_9999" in result.output


def test_dry_run_does_not_write_file(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app, ["rule-from", str(log), "--event", "evt_0002", "-o", str(output), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not output.exists()
    assert "후보 규칙" in result.output


def test_creates_new_rules_file(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"

    result = runner.invoke(app, ["rule-from", str(log), "--event", "evt_0002", "-o", str(output)])

    assert result.exit_code == 0, result.output
    assert output.exists()

    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    rule = doc["rule"]
    assert rule["id"] == "rule_0001"
    assert rule["origin"] == "auto"
    assert rule["then"] == "deny"
    assert rule["provenance"]["born_from"] == "evt_0002"
    assert rule["provenance"]["blocks"] == ["evt_0002"]
    assert rule["provenance"]["regressions"] == []


def test_appends_to_existing_rules_file(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"
    output.write_text(
        "rule:\n  id: rule_0001\n  origin: auto\n  when: {tool: other_tool}\n  then: deny\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["rule-from", str(log), "--event", "evt_0002", "-o", str(output)])

    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    assert text.count("---") == 1

    docs = list(yaml.safe_load_all(text))
    assert len(docs) == 2
    assert docs[0]["rule"]["id"] == "rule_0001"
    assert docs[1]["rule"]["id"] == "rule_0002"


def test_golden_negatives_produce_role_scoped_rule(tmp_path):
    """--golden에 다른 role의 같은 class(DDL_DESTRUCTIVE) 호출이 섞여 있으면
    depth3(agent.role 스코프)으로 좁혀진 규칙이 나온다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(
                1,
                "execute_sql",
                "DROP TABLE users;",
                verdict="allow",
                severity="critical",
                role="content_editor",
            ),
        ],
    )
    golden = tmp_path / "golden_run.jsonl"
    _write_jsonl(
        golden,
        [
            _tool_wrap(1, "execute_sql", "DROP TABLE tmp_scratch;", role="dba"),
        ],
    )
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app,
        ["rule-from", str(log), "--event", "evt_0001", "--golden", str(golden), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    rule = doc["rule"]
    assert rule["scope"] == {"agent.role": "content_editor"}
    assert rule["provenance"]["generality_rank"] == "3/3"
    assert rule["provenance"]["validated_against"] == str(golden)


def test_missing_log_file_errors(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"

    result = runner.invoke(app, ["rule-from", str(missing), "--event", "evt_0001"])

    assert result.exit_code == 1
    assert "없습니다" in result.output

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rein.cli import app

runner = CliRunner()


def _write_jsonl(
    path: Path,
    events: list[dict],
) -> None:
    with path.open("w", encoding="utf-8") as file:
        for event in events:
            file.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                )
                + "\n"
            )


def _tool_event(
    seq: int,
    event_id: str,
    query: str,
    *,
    role: str = "content_editor",
) -> dict:
    return {
        "schema_version": "v1",
        "evt": event_id,
        "seq": seq,
        "source": "tool_wrap",
        "parent_seq": None,
        "tool_name": "execute_sql",
        "args": {
            "query": query,
        },
        "context": {
            "agent_role": role,
        },
        "verdict": "allow",
    }


def _outcome_event(
    seq: int,
    event_id: str,
    severity: str,
) -> dict:
    return {
        "schema_version": "v1",
        "evt": event_id,
        "seq": seq,
        "source": "outcome",
        "parent_seq": seq,
        "tool_name": "execute_sql",
        "outcome": {
            "status": "ok",
            "side_effect": None,
            "severity": severity,
            "detail": None,
        },
    }


def _model_event(
    event_id: str,
    parent_seq: int,
    query: str,
) -> dict:
    return {
        "schema_version": "v1",
        "evt": event_id,
        "seq": None,
        "source": "model_client",
        "parent_seq": parent_seq,
        "tool_name": "execute_sql",
        "args": {
            "query": query,
        },
        "context": {},
        "verdict": None,
    }


@pytest.fixture
def report_inputs(
    tmp_path: Path,
) -> tuple[Path, Path]:
    golden_path = tmp_path / "golden_run.jsonl"

    _write_jsonl(
        golden_path,
        [
            _tool_event(
                1,
                "golden_0001",
                "SELECT * FROM posts WHERE id = 1;",
            ),
            _outcome_event(
                1,
                "golden_0001",
                "info",
            ),
            _tool_event(
                2,
                "golden_0002",
                "UPDATE posts SET body = 'safe' WHERE id = 1;",
            ),
            _outcome_event(
                2,
                "golden_0002",
                "info",
            ),
        ],
    )

    log_path = tmp_path / "run.jsonl"

    _write_jsonl(
        log_path,
        [
            _model_event(
                "model_0001",
                1,
                "SELECT * FROM posts WHERE id = 1;",
            ),
            _tool_event(
                1,
                "evt_0001",
                "SELECT * FROM posts WHERE id = 1;",
            ),
            _outcome_event(
                1,
                "evt_0001",
                "info",
            ),
            _model_event(
                "model_0002",
                2,
                "DROP TABLE users;",
            ),
            _tool_event(
                2,
                "evt_0042",
                "DROP TABLE users;",
            ),
            _outcome_event(
                2,
                "evt_0042",
                "critical",
            ),
        ],
    )

    rule_document = {
        "rule": {
            "id": "rule_0001",
            "origin": "auto",
            "when": {
                "tool": "execute_sql",
                "features": {
                    "class": {
                        "in": [
                            "DDL_DESTRUCTIVE",
                        ]
                    }
                },
            },
            "then": "deny",
            "rationale": "파괴적 DDL 실행을 차단함",
            "provenance": {
                "born_from": "evt_0042",
                "validated_against": golden_path.name,
                "blocks": [
                    "evt_0042",
                ],
                "regressions": [],
                "generality_rank": "2/3",
                "extractor": "sqlglot",
                "tool_sig": "execute_sql:test",
                "feature_schema": "v1",
            },
        }
    }

    rules_path = tmp_path / "rules.yaml"

    rules_path.write_text(
        yaml.safe_dump(
            rule_document,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    return log_path, rules_path


# report 명령어가 필수 옵션을 올바르게 파싱하는지 확인
def test_report_parses_required_options(
    report_inputs: tuple[Path, Path],
    tmp_path: Path,
):
    log, rules = report_inputs
    output = tmp_path / "report.html"
    result = runner.invoke(
        app,
        ["report", str(log), "--rules", str(rules), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "리포트를" in result.output


# report 명령어가 --output (long form) 옵션을 올바르게 받아들이는지 확인
def test_report_accepts_long_output_option(
    report_inputs: tuple[Path, Path],
    tmp_path: Path,
):
    log, rules = report_inputs
    output = tmp_path / "custom.html"

    result = runner.invoke(
        app,
        ["report", str(log), "--rules", str(rules), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "리포트를" in result.output


# report 명령어가 --rules 옵션을 필수로 요구하는지 확인
def test_report_requires_rules(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    output = tmp_path / "report.html"

    result = runner.invoke(
        app,
        ["report", str(log), "-o", str(output)],
    )

    assert result.exit_code != 0
    assert "rules" in result.output.lower()
    assert not output.exists()

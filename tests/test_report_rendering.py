"""report.html 데이터 생성·렌더링·CLI 연결 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rein.cli import app
from rein.report import build_report_data, render_report

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


def test_build_report_data_creates_timeline_and_metrics(
    report_inputs: tuple[Path, Path],
) -> None:
    log_path, rules_path = report_inputs

    report = build_report_data(
        log_path=log_path,
        rules_path=rules_path,
    )

    assert report.intervention_seq == 2
    assert report.metrics.total_events == 2
    assert report.metrics.critical_off == 1
    assert report.metrics.blocked_on == 1
    assert report.metrics.changed_count == 1

    failure_row = next(row for row in report.timeline if row.event_id == "evt_0042")

    assert failure_row.phase == "intervention"
    assert failure_row.off_verdict == "allow"
    assert failure_row.on_verdict == "deny"
    assert failure_row.matched_rule_ids == ("rule_0001",)


def test_build_report_data_creates_candidate_table(
    report_inputs: tuple[Path, Path],
) -> None:
    log_path, rules_path = report_inputs

    report = build_report_data(
        log_path=log_path,
        rules_path=rules_path,
    )

    analysis = report.rule_analyses[0]

    assert analysis.rule_id == "rule_0001"
    assert analysis.born_from == "evt_0042"

    assert [row.depth for row in analysis.candidate_rows] == [
        1,
        2,
        3,
    ]

    selected_rows = [row for row in analysis.candidate_rows if row.selected]

    assert len(selected_rows) == 1
    assert selected_rows[0].depth == 2


def test_build_report_data_creates_regression_matrix(
    report_inputs: tuple[Path, Path],
) -> None:
    log_path, rules_path = report_inputs

    report = build_report_data(
        log_path=log_path,
        rules_path=rules_path,
    )

    matrix = report.rule_analyses[0].matrix_rows

    positive = next(row for row in matrix if row.corpus_type == "positive")

    negatives = [row for row in matrix if row.corpus_type == "negative"]

    assert positive.event_id == "evt_0042"
    assert positive.label == "Blocked"

    assert len(negatives) == 2
    assert all(row.label == "Pass" for row in negatives)


def test_render_report_creates_four_sections(
    report_inputs: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    log_path, rules_path = report_inputs

    report = build_report_data(
        log_path=log_path,
        rules_path=rules_path,
    )

    output_path = tmp_path / "nested" / "report.html"

    render_report(
        data=report,
        output_path=output_path,
    )

    assert output_path.exists()

    html = output_path.read_text(encoding="utf-8")

    assert "① 분기 타임라인" in html
    assert "② Before / After 지표" in html
    assert "③ 후보 규칙 회귀 표" in html
    assert "④ 채택 규칙 회귀 매트릭스" in html

    assert "evt_0042" in html
    assert "rule_0001" in html
    assert "Blocked" in html
    assert "Pass" in html


def test_report_cli_creates_html(
    report_inputs: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    log_path, rules_path = report_inputs
    output_path = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "report",
            str(log_path),
            "--rules",
            str(rules_path),
            "-o",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "리포트를" in result.output


def test_report_cli_accepts_long_output_option(
    report_inputs: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    log_path, rules_path = report_inputs
    output_path = tmp_path / "custom.html"

    result = runner.invoke(
        app,
        [
            "report",
            str(log_path),
            "--rules",
            str(rules_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_path.exists()
    assert "리포트를" in result.output

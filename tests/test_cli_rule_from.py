"""`rein rule-from` CLI 테스트 (CLAUDE.md §4, 이슈 #10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from rein.cli import _cold_start_negatives, _recomputed_severity, app

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
            _tool_wrap(0, "execute_sql", "SELECT * FROM posts;", role="content_editor"),
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
        app, ["rule-from", str(log), "--event", "evt_0001", "-o", str(output), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not output.exists()
    assert "후보 규칙" in result.output


def test_creates_new_rules_file(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"

    result = runner.invoke(app, ["rule-from", str(log), "--event", "evt_0001", "-o", str(output)])

    assert result.exit_code == 0, result.output
    assert output.exists()

    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    rule = doc["rule"]
    assert rule["id"] == "rule_0001"
    assert rule["origin"] == "auto"
    assert rule["then"] == "deny"
    assert rule["provenance"]["born_from"] == "evt_0001"
    assert rule["provenance"]["blocks"] == ["evt_0001"]
    assert rule["provenance"]["regressions"] == []
    # candidate_trail은 코퍼스 크기에 비례해 커질 수 있어(§8 나머지 필드와
    # 성격이 다름) rules.yaml에는 쓰지 않는다 — dry-run 콘솔 출력 전용.
    assert "candidate_trail" not in rule["provenance"]


def test_dry_run_shows_candidate_trail_but_does_not_persist_it(tmp_path):
    """--dry-run 콘솔에는 depth별 후보 회귀 표가 보이지만, 그 데이터는
    rules.yaml provenance에 영구 기록되지 않는다(위 test_creates_new_rules_file과
    대칭 — 같은 값이 dry-run에서는 보이되 파일에는 안 남아야 함)."""
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app, ["rule-from", str(log), "--event", "evt_0001", "-o", str(output), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not output.exists()
    assert "후보별 회귀" in result.output


def test_appends_to_existing_rules_file(tmp_path):
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"
    output.write_text(
        "rule:\n  id: rule_0001\n  origin: auto\n  when: {tool: other_tool}\n  then: deny\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["rule-from", str(log), "--event", "evt_0001", "-o", str(output)])

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
                0,
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
            _tool_wrap(0, "execute_sql", "DROP TABLE tmp_scratch;", role="dba"),
        ],
    )
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app,
        ["rule-from", str(log), "--event", "evt_0000", "--golden", str(golden), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    rule = doc["rule"]
    assert rule["scope"] == {"agent.role": "content_editor"}
    assert rule["provenance"]["generality_rank"] == "3/3"
    assert rule["provenance"]["validated_against"] == str(golden)


def test_all_depths_regress_blocks_write_and_exits_1(tmp_path):
    """born_from에 agent_role이 없으면 depth3(scope)이 생성되지 않는다. 이때 --golden
    음성 코퍼스에 같은 tool+class(DDL_DESTRUCTIVE) 호출이 섞여 있으면 depth1도
    depth2도 회귀를 피할 수 없다 — §7 "음성 0회귀" 채택 기준을 만족하는 후보가
    없으므로 rules.yaml에 쓰지 않고 exit 1이어야 한다(조용한 통과 금지)."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", "DROP TABLE users;", verdict="allow", severity="critical"),
        ],
    )
    golden = tmp_path / "golden_run.jsonl"
    _write_jsonl(
        golden,
        [
            _tool_wrap(0, "execute_sql", "DROP TABLE tmp_scratch;"),
        ],
    )
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app,
        ["rule-from", str(log), "--event", "evt_0000", "--golden", str(golden), "-o", str(output)],
    )

    assert result.exit_code == 1
    assert not output.exists()
    assert "회귀" in result.output


def test_all_depths_regress_dry_run_still_shows_matrix(tmp_path):
    """--dry-run은 회귀가 남아도 exit 0으로 후보/매트릭스만 보여주고 끝난다
    (파일에 쓰지 않는 건 원래도 dry-run의 동작이라 회귀 게이트와 무관)."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", "DROP TABLE users;", verdict="allow", severity="critical"),
        ],
    )
    golden = tmp_path / "golden_run.jsonl"
    _write_jsonl(
        golden,
        [
            _tool_wrap(0, "execute_sql", "DROP TABLE tmp_scratch;"),
        ],
    )
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app,
        [
            "rule-from",
            str(log),
            "--event",
            "evt_0000",
            "--golden",
            str(golden),
            "-o",
            str(output),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not output.exists()
    assert "회귀" in result.output


def test_missing_log_file_errors(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"

    result = runner.invoke(app, ["rule-from", str(missing), "--event", "evt_0001"])

    assert result.exit_code == 1
    assert "없습니다" in result.output


# ── featurizer guard (이슈 #10): 로그의 outcome.severity를 신뢰하지 않는다 ────


def test_recomputed_severity_ignores_mistagged_log_value():
    """로그에 severity="info"로 잘못 찍혀 있어도, 실제 쿼리가 DROP TABLE이면
    featurize 재계산 결과(critical)를 따른다 — 로그 값을 그대로 믿지 않는다."""
    mistagged = _tool_wrap(0, "execute_sql", "DROP TABLE users;", severity="info")
    assert _recomputed_severity(mistagged) == "critical"


def test_recomputed_severity_none_for_non_sql():
    non_sql = {"tool_name": "delete_file", "args": {"path": "/tmp/x"}}
    assert _recomputed_severity(non_sql) is None


def test_cold_start_negatives_excludes_mistagged_destructive_event():
    """음성 코퍼스 선정 단계에서도 재계산 severity를 쓴다 — 로그에 severity="info"로
    잘못 태깅된 DROP TABLE 호출은 음성 후보에서 제외되어야 한다."""
    born_from = _tool_wrap(
        1, "execute_sql", "DROP TABLE users;", severity="critical", role="content_editor"
    )
    mistagged_negative = _tool_wrap(
        0, "execute_sql", "DROP TABLE other_table;", severity="info", role="dba"
    )

    negatives = _cold_start_negatives([mistagged_negative, born_from], born_from)

    assert negatives == []


# ── 권한 테이블 기반 합성 음성 (§5.2, 이슈 #11) ──────────────────────────────


def test_rule_from_uses_permission_table_when_no_golden(tmp_path):
    """--golden도 없고 log에 다른 호출도 전혀 없어도, --config로 넘긴 rein.yaml의
    permissions 섹션만으로 depth2(tool+class)까지 안전하게 일반화된다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(
                0,
                "execute_sql",
                "DROP TABLE users;",
                verdict="allow",
                severity="critical",
                role="content_editor",
            ),
        ],
    )
    config = tmp_path / "rein.yaml"
    config.write_text(
        "permissions:\n  content_editor:\n    execute_sql: [SQL_SAFE]\n",
        encoding="utf-8",
    )
    output = tmp_path / "rules.yaml"

    result = runner.invoke(
        app,
        [
            "rule-from",
            str(log),
            "--event",
            "evt_0000",
            "-o",
            str(output),
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    rule = doc["rule"]
    assert rule["provenance"]["generality_rank"] == "2/3"
    assert rule["provenance"]["regressions"] == []
    assert "permissions" in rule["provenance"]["validated_against"]


def test_rule_from_without_permissions_section_falls_back_to_log_only(tmp_path):
    """rein.yaml이 없으면(기본 경로에 파일 자체가 없음) 조용히 스킵되고 기존
    log 기반 negatives만으로 동작한다 — 파일 부재가 rule-from 자체를 막지 않는다."""
    log = tmp_path / "run.jsonl"
    _run_log_with_failure(log)
    output = tmp_path / "rules.yaml"
    missing_config = tmp_path / "no_such_rein.yaml"

    result = runner.invoke(
        app,
        [
            "rule-from",
            str(log),
            "--event",
            "evt_0001",
            "-o",
            str(output),
            "--config",
            str(missing_config),
        ],
    )

    assert result.exit_code == 0, result.output
    doc = next(yaml.safe_load_all(output.read_text(encoding="utf-8")))
    validated_against = doc["rule"]["provenance"]["validated_against"]
    assert str(log) in validated_against
    assert "permissions" not in validated_against
    assert "cold-start subset" in validated_against
    assert "born_from=evt_0001" in validated_against


def test_cold_start_negatives_excludes_non_sql_even_if_tagged_info():
    """featurize가 실패하는(비-SQL) 이벤트는 로그 severity가 "info"여도
    검증 불가로 간주해 자동 제외된다."""
    born_from = {
        "evt": "evt_0001",
        "tool_name": "delete_file",
        "verdict": "allow",
        "args": {"path": "/tmp/target"},
        "outcome": {"severity": "critical"},
    }
    non_sql_negative = {
        "evt": "evt_0000",
        "tool_name": "delete_file",
        "verdict": "allow",
        "args": {"path": "/tmp/other"},
        "outcome": {"severity": "info"},
    }

    negatives = _cold_start_negatives([non_sql_negative, born_from], born_from)

    assert negatives == []

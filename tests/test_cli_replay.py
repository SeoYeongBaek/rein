"""rein replay CLI 테스트 (CLAUDE.md §4)."""

import json
import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rein.cli import app

runner = CliRunner()


def _write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _tool_wrap(seq: int, tool_name: str, args: dict, verdict: str = "allow") -> dict:
    return {
        "schema_version": "v1",
        "evt": f"evt_{seq:04d}",
        "seq": seq,
        "source": "tool_wrap",
        "tool_name": tool_name,
        "args": args,
        "verdict": verdict,
        "outcome": {"status": "ok", "severity": "info", "detail": ""},
    }


def _write_rule(path: Path, tool: str, then: str) -> None:
    path.write_text(
        yaml.dump({"rule": {"id": "rule_test", "when": {"tool": tool}, "then": then}}),
        encoding="utf-8",
    )


# ── 기본 동작 ─────────────────────────────────────────────────────────────────


def test_replay_lists_events(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
            _tool_wrap(1, "read_file", {"path": "/tmp/x"}),
        ],
    )
    result = runner.invoke(app, ["replay", str(log)])
    assert result.exit_code == 0
    assert "execute_sql" in result.output
    assert "read_file" in result.output


def test_replay_lists_events_with_unstyled_severity(tmp_path):
    """severity가 critical/warning/info 중 하나가 아니면 rich 마크업 태그가
    빈 스타일("[]...[/]")이 되어 MarkupError로 크래시하던 버그의 회귀 테스트."""
    log = tmp_path / "run.jsonl"
    evt = _tool_wrap(0, "execute_sql", {"query": "SELECT 1"})
    evt["outcome"] = {"status": "ok", "severity": "unknown", "detail": ""}
    _write_jsonl(log, [evt])

    result = runner.invoke(app, ["replay", str(log)])
    assert result.exit_code == 0
    assert "unknown" in result.output


def test_replay_missing_file_exits_1(tmp_path):
    result = runner.invoke(app, ["replay", str(tmp_path / "no.jsonl")])
    assert result.exit_code == 1


def test_replay_invalid_mode_rejected(tmp_path):
    """--mode는 ReplayMode(verify/live)로 타입돼 있어 잘못된 값은 typer/click이
    함수 본문 진입 전에 거른다 (exit code 2 = click UsageError)."""
    log = tmp_path / "run.jsonl"
    log.touch()
    result = runner.invoke(app, ["replay", str(log), "--mode", "invalid"])
    assert result.exit_code == 2


# ── --mode live ───────────────────────────────────────────────────────────────


def test_replay_live_mode_prints_warning(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    result = runner.invoke(app, ["replay", str(log), "--mode", "live"])
    assert result.exit_code == 0
    assert "정직한 한계" in result.output


# ── --compare ─────────────────────────────────────────────────────────────────


def test_compare_no_rules_shows_no_change(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    result = runner.invoke(app, ["replay", str(log), "--compare"])
    assert result.exit_code == 0
    assert "CHANGED" not in result.output


def test_compare_with_matching_rule_shows_changed(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"})])

    rules = tmp_path / "rules.yaml"
    _write_rule(rules, tool="execute_sql", then="deny")

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "CHANGED" in result.output
    assert "deny" in result.output


def test_compare_with_non_matching_rule_shows_no_change(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "read_file", {"path": "/tmp/x"})])

    rules = tmp_path / "rules.yaml"
    _write_rule(rules, tool="execute_sql", then="deny")

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "CHANGED" not in result.output


def test_compare_scope_agent_role_filters_out_non_matching(tmp_path):
    """scope.agent.role이 이벤트 context.agent_role과 다르면 규칙이 적용되지 않는다."""
    log = tmp_path / "run.jsonl"
    evt = _tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"})
    evt["context"] = {"agent_role": "admin"}
    _write_jsonl(log, [evt])

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        yaml.dump(
            {
                "rule": {
                    "id": "rule_test",
                    "when": {"tool": "execute_sql"},
                    "scope": {"agent.role": "content_editor"},
                    "then": "deny",
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "CHANGED" not in result.output


def test_compare_scope_agent_role_matches(tmp_path):
    log = tmp_path / "run.jsonl"
    evt = _tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"})
    evt["context"] = {"agent_role": "content_editor"}
    _write_jsonl(log, [evt])

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        yaml.dump(
            {
                "rule": {
                    "id": "rule_test",
                    "when": {"tool": "execute_sql"},
                    "scope": {"agent.role": "content_editor"},
                    "then": "deny",
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "CHANGED" in result.output


def test_compare_rules_doc_missing_rule_key_warns_and_is_skipped(tmp_path):
    """'rule' 키가 없는 문서를 조용히 무시하면 규칙이 누락된 채 비교가 진행될 수
    있다 — 경고를 남기고, 유효한 나머지 규칙은 정상 로드되는지 확인."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"})])

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        yaml.dump({"not_rule": {"id": "r1"}})
        + "---\n"
        + yaml.dump({"rule": {"id": "r2", "when": {"tool": "execute_sql"}, "then": "deny"}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "CHANGED" in result.output  # r2는 여전히 정상 적용됨


def test_compare_multi_doc_rules_file_appends(tmp_path):
    """rein rule-from의 append 동작을 받아내는 형태 — 파일 하나에 규칙 여러 개(--- 구분)."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
            _tool_wrap(1, "delete_file", {"path": "/tmp/x"}),
        ],
    )

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        yaml.dump({"rule": {"id": "r1", "when": {"tool": "execute_sql"}, "then": "deny"}})
        + "---\n"
        + yaml.dump({"rule": {"id": "r2", "when": {"tool": "delete_file"}, "then": "approve"}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    # 두 이벤트 모두 규칙에 걸려 판정이 바뀌어야 함 (두 번째 문서 무시되면 1건만 CHANGED)
    assert result.output.count("CHANGED") == 2
    assert "총 2개 이벤트 중 2개 판정 변경" in result.output


def test_compare_conflicting_rules_pick_most_restrictive(tmp_path):
    """같은 이벤트에 여러 규칙이 매칭되면 deny > approve > retry > allow 중 가장 제한적인 것."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"})])

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        yaml.dump({"rule": {"id": "r1", "when": {"tool": "execute_sql"}, "then": "approve"}})
        + "---\n"
        + yaml.dump({"rule": {"id": "r2", "when": {"tool": "execute_sql"}, "then": "deny"}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    assert "deny" in result.output


def test_compare_counts_changed_events(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", {"query": "DROP TABLE users;"}),
            _tool_wrap(1, "execute_sql", {"query": "SELECT 1"}),
            _tool_wrap(2, "read_file", {"path": "/tmp/x"}),
        ],
    )
    rules = tmp_path / "rules.yaml"
    _write_rule(rules, tool="execute_sql", then="deny")

    result = runner.invoke(app, ["replay", str(log), "--compare", "--rules", str(rules)])
    assert result.exit_code == 0
    match = re.search(r"총 (\d+)개 이벤트 중 (\d+)개 판정 변경", result.output)
    assert match is not None
    assert match.group(1) == "3"  # 전체 이벤트 수
    assert match.group(2) == "2"  # CHANGED 수

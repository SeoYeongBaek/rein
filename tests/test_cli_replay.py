"""rein replay CLI 테스트 (CLAUDE.md §4)."""

import json
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


def test_replay_missing_file_exits_1(tmp_path):
    result = runner.invoke(app, ["replay", str(tmp_path / "no.jsonl")])
    assert result.exit_code == 1


def test_replay_invalid_mode_exits_1(tmp_path):
    log = tmp_path / "run.jsonl"
    log.touch()
    result = runner.invoke(app, ["replay", str(log), "--mode", "invalid"])
    assert result.exit_code == 1


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
    assert "2개" in result.output  # 2개 CHANGED

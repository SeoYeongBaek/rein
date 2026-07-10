import json
from pathlib import Path

from typer.testing import CliRunner

from rein.cli import app


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _tool_wrap(seq: int, evt: str, tool_name: str = "execute_sql", verdict: str = "allow") -> dict:
    return {
        "schema_version": "v1",
        "evt": evt,
        "seq": seq,
        "source": "tool_wrap",
        "parent_seq": None,
        "tool_name": tool_name,
        "args": {"query": "SELECT 1"},
        "context": {"task": "t", "agent_role": "editor"},
        "verdict": verdict,
    }


def _outcome(evt_id: str, seq: int, tool_name: str, severity: str = "info") -> dict:
    return {
        "schema_version": "v1",
        "evt": evt_id,
        "seq": seq,
        "source": "outcome",
        "parent_seq": seq,
        "tool_name": tool_name,
        "outcome": {"status": "ok", "severity": severity, "detail": "ok"},
    }


def test_seed_valid_run_passes(tmp_path: Path):
    run = tmp_path / "run.jsonl"
    _write_jsonl(
        run,
        [
            _tool_wrap(0, "evt_0001"),
            _outcome("evt_0001", 0, "execute_sql"),
            _tool_wrap(1, "evt_0002"),
            _outcome("evt_0002", 1, "execute_sql"),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 0, result.output
    assert "검증 통과" in result.output
    # §4: 골든 트레이스 = 입력 run.jsonl 그 자체 (별도 복사 없음).
    # 메시지가 실제 입력 경로를 안내해야 사용자가 --golden에 그대로 쓸 수 있다.
    assert str(run) in result.output


def test_seed_rejects_critical_outcome(tmp_path: Path):
    run = tmp_path / "run.jsonl"
    _write_jsonl(
        run,
        [
            _tool_wrap(0, "evt_0001"),
            _outcome("evt_0001", 0, "execute_sql", severity="critical"),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 1
    assert "critical" in result.output


def test_seed_rejects_missing_file(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(tmp_path / "missing.jsonl")])

    assert result.exit_code == 1
    assert "없습니다" in result.output


def test_seed_rejects_zero_tool_wrap_events(tmp_path: Path):
    run = tmp_path / "run.jsonl"
    # 빈 파일
    run.write_text("")

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 1
    assert "0건" in result.output


def test_seed_rejects_bad_schema_version(tmp_path: Path):
    run = tmp_path / "run.jsonl"
    bad = _tool_wrap(0, "evt_0001")
    bad["schema_version"] = "v999"
    _write_jsonl(run, [bad])

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 1
    assert "schema_version" in result.output


def test_seed_rejects_malformed_json(tmp_path: Path):
    run = tmp_path / "run.jsonl"
    run.write_text("not-json\n")

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 1
    assert "파싱 실패" in result.output


def test_seed_skips_model_client_lines(tmp_path: Path):
    """§9 model_client 이벤트는 검증 스킵 (스키마 표 외)."""
    run = tmp_path / "run.jsonl"
    _write_jsonl(
        run,
        [
            {
                "schema_version": "v1",
                "evt": "evt_0001",
                "source": "model_client",
                "seq": None,
                "parent_seq": 0,
                "tool_name": "execute_sql",
                "args": {},
                "context": {},
                "verdict": None,
            },
            _tool_wrap(0, "evt_0002"),
            _outcome("evt_0002", 0, "execute_sql"),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["seed", str(run)])

    assert result.exit_code == 0, result.output

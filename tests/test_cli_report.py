from pathlib import Path

from typer.testing import CliRunner

from rein.cli import app

runner = CliRunner()


# report 명령어가 필수 옵션을 올바르게 파싱하는지 확인
def test_report_parses_required_options(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    rules = tmp_path / "rules.yaml"
    output = tmp_path / "report.html"

    result = runner.invoke(
        app,
        ["report", str(log), "--rules", str(rules), "-o", str(output)],
    )

    assert result.exit_code == 0
    assert "report 옵션 파싱 완료" in result.output
    assert str(log) in result.output
    assert str(rules) in result.output
    assert str(output) in result.output
    assert not output.exists()


# report 명령어가 --output (long form) 옵션을 올바르게 받아들이는지 확인
def test_report_accepts_long_output_option(tmp_path: Path):
    log = tmp_path / "run.jsonl"
    rules = tmp_path / "rules.yaml"
    output = tmp_path / "custom.html"

    result = runner.invoke(
        app,
        ["report", str(log), "--rules", str(rules), "--output", str(output)],
    )

    assert result.exit_code == 0
    assert str(output) in result.output
    assert not output.exists()


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

"""Harness mode="live-rerun" 배선 테스트 (이슈 #30, CLAUDE.md §4/§6).

register_tool의 wrapper가 실제 함수 호출 전에 ReplayEngine.match()로
위치 매칭을 검증하는지 확인한다. ReplayEngine 자체의 매칭 규칙(값 비교
없음, model_client 제외 등)은 tests/test_replay.py에서 이미 검증했으므로
여기서는 Harness ↔ ReplayEngine 배선만 다룬다.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein.harness import Harness
from rein.replay.engine import ReplayMismatchError


def _write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _tool_wrap(seq: int, tool_name: str, args: dict) -> dict:
    return {
        "schema_version": "v1",
        "evt": f"evt_{seq:04d}",
        "seq": seq,
        "source": "tool_wrap",
        "tool_name": tool_name,
        "args": args,
        "verdict": "allow",
        "outcome": {"status": "ok", "severity": "info", "detail": ""},
    }


@pytest.fixture
def mock_stage_loaders():
    """도구가 파이프라인 없이(전부 ALLOW) 통과하도록 스테이지 로더를 모킹."""
    with (
        patch("rein.harness.load_stage_order", return_value=["schema"]),
        patch("rein.harness.resolve_stage_order", return_value=["schema"]),
    ):
        yield


def test_live_rerun_wires_replay_engine(mock_stage_loaders, tmp_path):
    """mode="live-rerun" 생성 시 ReplayEngine이 로그를 로드해 보관된다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "add", {"a": 1, "b": 2})])

    h = Harness(record=tmp_path / "out.jsonl", mode="live-rerun", replay_from=log)

    assert h._replay_engine is not None
    assert h._replay_engine.mode == "live-rerun"
    assert len(h._replay_engine) == 1


def test_live_rerun_position_match_allows_real_call(mock_stage_loaders, tmp_path):
    """녹화된 시퀀스와 일치하면 실제 함수가 호출된다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "add", {"a": 1, "b": 2})])

    h = Harness(record=tmp_path / "out.jsonl", mode="live-rerun", replay_from=log)

    @h.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3
    assert h._replay_engine._cursor == 1


def test_live_rerun_tool_name_mismatch_blocks_real_call(mock_stage_loaders, tmp_path):
    """로그와 다른 tool_name이면 ReplayMismatchError, 실제 함수는 호출되지 않는다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "delete_file", {"path": "/tmp/x"})])

    h = Harness(record=tmp_path / "out.jsonl", mode="live-rerun", replay_from=log)

    called = False

    @h.register_tool
    def add(a: int, b: int) -> int:
        nonlocal called
        called = True
        return a + b

    with pytest.raises(ReplayMismatchError, match="tool_name 불일치"):
        add(1, 2)

    assert not called


def test_record_mode_does_not_attach_replay_engine(mock_stage_loaders, tmp_path):
    """mode="record"(기본)에서는 ReplayEngine이 아예 붙지 않는다(회귀 방지)."""
    h = Harness(record=tmp_path / "out.jsonl")

    assert h._replay_engine is None

    @h.register_tool
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3

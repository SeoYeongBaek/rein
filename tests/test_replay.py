"""ReplayEngine 3모드 테스트 (CLAUDE.md §6)."""

import json
import warnings
from pathlib import Path

import pytest

from rein.replay.engine import ReplayEngine, ReplayMismatchError


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


def _model_client(parent_seq: int) -> dict:
    return {
        "schema_version": "v1",
        "evt": f"evt_mc_{parent_seq}",
        "seq": None,
        "source": "model_client",
        "parent_seq": parent_seq,
        "tool_name": "execute_sql",
        "args": {"query": "DROP TABLE users;"},
    }


# ── record 모드 ──────────────────────────────────────────────────────────────


def test_record_mode_match_raises(tmp_path):
    log = tmp_path / "run.jsonl"
    log.touch()
    engine = ReplayEngine(log, mode="record")
    with pytest.raises(RuntimeError):
        engine.match("execute_sql", {"query": "SELECT 1"})


def test_record_mode_does_not_load_events(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    engine = ReplayEngine(log, mode="record")
    assert len(engine) == 0


# ── replay-verify 모드 ───────────────────────────────────────────────────────


def test_replay_verify_loads_tool_wrap_only(tmp_path):
    """source: model_client 이벤트는 매칭 대상에서 제외."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _model_client(0),
            _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
            _model_client(1),
            _tool_wrap(1, "delete_file", {"path": "/tmp/x"}),
        ],
    )
    engine = ReplayEngine(log, mode="replay-verify")
    assert len(engine) == 2


def test_replay_verify_position_match(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
            _tool_wrap(1, "delete_file", {"path": "/tmp/x"}),
        ],
    )
    engine = ReplayEngine(log, mode="replay-verify")

    evt0 = engine.match("execute_sql", {"query": "DROP TABLE users;"})
    assert evt0["tool_name"] == "execute_sql"
    assert evt0["seq"] == 0

    evt1 = engine.match("delete_file", {"path": "/keep/this"})
    assert evt1["seq"] == 1


def test_replay_verify_tool_name_mismatch_hard_error(tmp_path):
    """tool_name 불일치 → 즉시 하드 에러."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    engine = ReplayEngine(log, mode="replay-verify")

    with pytest.raises(ReplayMismatchError, match="tool_name 불일치"):
        engine.match("delete_file", {"query": "SELECT 1"})


def test_replay_verify_args_key_mismatch_hard_error(tmp_path):
    """args 키 집합 불일치 → 즉시 하드 에러."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    engine = ReplayEngine(log, mode="replay-verify")

    with pytest.raises(ReplayMismatchError, match="args 키 집합 불일치"):
        engine.match("execute_sql", {"sql": "SELECT 1"})


def test_replay_verify_args_value_difference_is_not_error(tmp_path):
    """값이 달라도 키 집합이 같으면 통과 — 값 비교 로직 없음."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    engine = ReplayEngine(log, mode="replay-verify")

    # 값이 완전히 다른 위험한 쿼리여도 키 집합이 같으면 통과
    evt = engine.match("execute_sql", {"query": "DROP TABLE users;"})
    assert evt is not None


def test_replay_verify_exhausted_raises(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])
    engine = ReplayEngine(log, mode="replay-verify")

    engine.match("execute_sql", {"query": "SELECT 1"})
    with pytest.raises(ReplayMismatchError, match="소진"):
        engine.match("execute_sql", {"query": "SELECT 1"})


def test_replay_verify_iter(tmp_path):
    log = tmp_path / "run.jsonl"
    events = [
        _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
        _tool_wrap(1, "delete_file", {"path": "/tmp/x"}),
    ]
    _write_jsonl(log, events)
    engine = ReplayEngine(log, mode="replay-verify")

    loaded = list(engine)
    assert len(loaded) == 2
    assert loaded[0]["tool_name"] == "execute_sql"
    assert loaded[1]["tool_name"] == "delete_file"


# ── live-rerun 모드 ───────────────────────────────────────────────────────────


def test_live_rerun_emits_warning(tmp_path):
    """live-rerun 모드는 정직한 한계 경고를 출력한다 (CLAUDE.md §6)."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ReplayEngine(log, mode="live-rerun")

    assert len(w) == 1
    assert "첫 개입 지점" in str(w[0].message)


def test_live_rerun_position_match(tmp_path):
    """live-rerun도 position 기반 매칭 동작."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        engine = ReplayEngine(log, mode="live-rerun")

    evt = engine.match("execute_sql", {"query": "anything"})
    assert evt["seq"] == 0


def test_live_rerun_tool_name_mismatch_hard_error(tmp_path):
    log = tmp_path / "run.jsonl"
    _write_jsonl(log, [_tool_wrap(0, "execute_sql", {"query": "SELECT 1"})])

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        engine = ReplayEngine(log, mode="live-rerun")

    with pytest.raises(ReplayMismatchError):
        engine.match("delete_file", {"query": "SELECT 1"})


# ── 공통 ──────────────────────────────────────────────────────────────────────


def test_model_client_has_no_seq_and_excluded(tmp_path):
    """seq=None인 model_client 이벤트는 매칭 카운터에 잡히지 않는다."""
    log = tmp_path / "run.jsonl"
    _write_jsonl(
        log,
        [
            _model_client(0),
            _model_client(0),
            _model_client(0),
            _tool_wrap(0, "execute_sql", {"query": "SELECT 1"}),
        ],
    )
    engine = ReplayEngine(log, mode="replay-verify")
    assert len(engine) == 1

    evt = engine.match("execute_sql", {"query": "SELECT 1"})
    assert evt["seq"] == 0

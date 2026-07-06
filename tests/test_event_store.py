"""EventStore M1 #5 — 완료 기준 + 설계 결정 검증.

이 테스트는 다음을 1:1 보장한다:
    [A] append-only, 단일 순번 카운터로 seq 부여
    [B] schema_version 필드 포함
    [C] outcome.detail 자유 텍스트 필드 포함
    [D] source=model_client 이벤트는 parent_seq만 가짐 (seq 없음)

범위 밖:
    - 실제 인터셉터 통합 (harness.py 쪽 PR 책임)
    - §7 severity 분류 테이블 (후속 PR)
    - Context 의미 필드 채움 (후속 PR)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from rein.events import (
    SCHEMA_VERSION,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    EventStore,
)

# ---------- 픽스처 ----------


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    """기본 EventStore. tmp_path 사용으로 격리."""
    return EventStore(tmp_path / "events.jsonl")


def _read_lines(path: Path) -> list[dict]:
    """JSONL 한 줄씩 파싱해 dict 리스트로 돌려준다."""
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------- [A] append-only, 단일 순번 ----------


class TestSeqCounter:
    """[A] append-only + 단일 순번(seq) 카운터 검증."""

    def test_seq_starts_at_zero_and_monotonically_increases(self, store: EventStore) -> None:
        evt1 = store.record_tool_wrap(tool_name="foo", args={"x": 1}, context=None, verdict="allow")
        evt2 = store.record_tool_wrap(tool_name="bar", args={"y": 2}, context=None, verdict="allow")
        assert evt1["seq"] == 1
        assert evt2["seq"] == 2

    def test_record_appends_new_lines_without_overwrite(self, tmp_path: Path) -> None:
        """[A] append-only: 매 호출이 정확히 한 줄을 추가한다."""
        path = tmp_path / "events.jsonl"
        s1 = EventStore(path)
        s1.record_tool_wrap(tool_name="a", args={}, context=None, verdict="allow")
        s1.close()

        # 같은 경로로 새 인스턴스 → append 모드로 열림 (덮어쓰기 X)
        s2 = EventStore(path)
        s2.record_tool_wrap(tool_name="b", args={}, context=None, verdict="allow")
        s2.close()

        lines = _read_lines(path)
        assert len(lines) == 2
        assert lines[0]["tool_name"] == "a"
        assert lines[1]["tool_name"] == "b"

    def test_each_record_writes_exactly_one_line(self, store: EventStore, tmp_path: Path) -> None:
        """[A] 한 record_*() 호출 = 정확히 한 줄."""
        store.record_tool_wrap(tool_name="x", args={}, context=None, verdict="allow")
        store.record_tool_wrap(tool_name="y", args={}, context=None, verdict="allow")
        path = store._path
        with path.open("r", encoding="utf-8") as f:
            non_empty = [ln for ln in f if ln.strip()]
        assert len(non_empty) == 2


# ---------- [B] schema_version ----------


class TestSchemaVersion:
    """[B] schema_version 필드 포함."""

    def test_tool_wrap_event_has_schema_version(self, store: EventStore) -> None:
        evt = store.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        assert evt["schema_version"] == SCHEMA_VERSION
        assert evt["schema_version"] == "v1"

    def test_model_client_event_has_schema_version(self, store: EventStore) -> None:
        evt = store.record_model_client(parent_seq=1, tool_name="foo", proposed_args={})
        assert evt["schema_version"] == SCHEMA_VERSION

    def test_outcome_event_has_schema_version(self, store: EventStore) -> None:
        evt = store.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        store.record_outcome(evt, status="ok", severity=SEVERITY_INFO, detail="ok")
        with store._path.open("r", encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        # 두 번째 줄이 outcome 라인
        assert lines[1]["schema_version"] == SCHEMA_VERSION


# ---------- [C] outcome.detail 자유 텍스트 ----------


class TestOutcomeDetail:
    """[C] outcome.detail 자유 텍스트 필드."""

    @pytest.mark.parametrize(
        "text",
        [
            "simple ascii",
            "한글 디테일 — 이벤트 본문",  # CLAUDE.md §14: 줄표 허용 (코드/문서)
            "with\nnewline",  # JSON 이스케이프되어도 detail 문자열로 보존
            'quote " and backslash \\',
            "🎯 emoji",
        ],
    )
    def test_detail_preserved_roundtrip(self, store: EventStore, text: str) -> None:
        evt = store.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        store.record_outcome(evt, status="error", severity=SEVERITY_WARNING, detail=text)
        store.close()
        lines = _read_lines(store._path)
        assert lines[1]["outcome"]["detail"] == text

    def test_record_error_sets_detail_from_exception(self, store: EventStore) -> None:
        """record_error 헬퍼는 예외 타입+메시지를 detail 기본값으로."""
        evt = store.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        exc = RuntimeError("boom")
        store.record_error(evt, exc)
        store.close()
        lines = _read_lines(store._path)
        assert lines[1]["outcome"]["detail"] == "RuntimeError: boom"


# ---------- [D] model_client: parent_seq만, seq 없음 ----------


class TestModelClientEvent:
    """[D] source=model_client 이벤트는 seq 없이 parent_seq만 가짐."""

    def test_model_client_seq_is_null(self, store: EventStore) -> None:
        evt = store.record_model_client(
            parent_seq=1, tool_name="foo", proposed_args={"q": "DROP TABLE"}
        )
        assert evt["source"] == "model_client"
        assert evt["seq"] is None  # [D] 명시적 null
        assert evt["parent_seq"] == 1

    def test_model_client_does_not_increase_tool_wrap_counter(self, store: EventStore) -> None:
        """[D] self._seq는 tool_wrap 전용. model_client는 seq를 부여하지
        않으므로 카운터를 올리지 않는다."""
        store.record_tool_wrap(
            tool_name="foo", args={}, context=None, verdict="allow"
        )  # tool_wrap seq=1
        store.record_model_client(
            parent_seq=1, tool_name="foo", proposed_args={}
        )  # model_client: seq 부여 X
        evt3 = store.record_tool_wrap(tool_name="bar", args={}, context=None, verdict="allow")
        # 두 번째 tool_wrap의 seq는 2 — model_client가 카운터를 안 올렸음
        assert evt3["seq"] == 2

    def test_model_client_evt_id_separate_namespace(self, store: EventStore) -> None:
        """[D 임시] model_client evt ID는 tool_wrap과 분리된 네임스페이스.
        PR 본문에서 (a)/(b) 결정 회피를 명시했음 — 본 테스트는 현 채택안(a)
        만을 잠근다. 후속 PR에서 단일화 결정 시 이 테스트는 수정된다."""
        evt_mc = store.record_model_client(parent_seq=None, tool_name="foo", proposed_args={})
        assert evt_mc["evt"] == "evt_mc_0001"
        assert not evt_mc["evt"].startswith("evt_") or evt_mc["evt"].startswith("evt_mc_")
        # ↑ 위 assertion은 evt_mc_0001은 evt_로 시작하지만 mc_ 세그먼트가
        # 바로 뒤에 붙음. 도식적 명확성을 위해 두 번째 줄로 분리:
        assert evt_mc["evt"].startswith("evt_mc_")
        assert evt_mc["evt"] != f"evt_{1:04d}"


# ---------- [D1] lazy open ----------


class TestLazyOpen:
    def test_file_created_on_first_record(self, tmp_path: Path) -> None:
        """[D1] 명시적 open() 없이 record_* 호출해도 파일 생성됨."""
        path = tmp_path / "lazy.jsonl"
        s = EventStore(path)
        assert not path.exists()  # 아직 안 열림
        s.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        assert path.exists()
        s.close()

    def test_close_then_record_reopens(self, tmp_path: Path) -> None:
        """[D1] close 후 record_* 호출이 idempotent하게 다시 연다."""
        path = tmp_path / "reopen.jsonl"
        s = EventStore(path)
        s.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        s.close()
        s.record_tool_wrap(tool_name="bar", args={}, context=None, verdict="allow")
        lines = _read_lines(path)
        assert len(lines) == 2


# ---------- [D3] evt ID 포맷 ----------


class TestEvtIdFormat:
    def test_tool_wrap_evt_id_zero_padded_4_digits(self, store: EventStore) -> None:
        evt = store.record_tool_wrap(tool_name="foo", args={}, context=None, verdict="allow")
        assert evt["evt"] == "evt_0001"

    def test_evt_id_does_not_collide_across_many_records(self, store: EventStore) -> None:
        ids = []
        for i in range(15):
            evt = store.record_tool_wrap(tool_name=f"t{i}", args={}, context=None, verdict="allow")
            ids.append(evt["evt"])
        assert ids == [f"evt_{i + 1:04d}" for i in range(15)]


# ---------- append-only 보존 (tool_wrap → outcome 분리) ----------


class TestAppendOnlyPreservation:
    """outcome은 tool_wrap 라인을 덮어쓰지 않고 새 라인으로 append."""

    def test_outcome_appends_new_line_not_overwrite(self, store: EventStore) -> None:
        evt = store.record_tool_wrap(tool_name="foo", args={"x": 1}, context=None, verdict="allow")
        store.record_outcome(evt, status="ok", severity=SEVERITY_INFO, detail="ok")
        store.close()
        lines = _read_lines(store._path)
        assert len(lines) == 2
        # 1줄: tool_wrap, outcome 없음
        assert "outcome" not in lines[0]
        # 2줄: outcome 본문, 같은 evt/seq 표면 보유
        assert lines[1]["evt"] == evt["evt"]
        assert lines[1]["seq"] == evt["seq"]
        assert lines[1]["outcome"]["status"] == "ok"


# ---------- thread-safety ----------


class TestThreadSafety:
    def test_concurrent_records_keep_monotonic_seq(self, store: EventStore) -> None:
        """동시 record에서도 seq 단조 증가. 락이 깨지지 않았다는 약한 증거."""
        n = 50
        barrier = threading.Barrier(n)

        def worker(i: int) -> None:
            barrier.wait()
            store.record_tool_wrap(tool_name=f"t{i}", args={}, context=None, verdict="allow")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        store.close()
        lines = _read_lines(store._path)
        seqs = sorted(ln["seq"] for ln in lines)
        assert seqs == list(range(1, n + 1))


# ---------- 스모크 ----------


class TestSmoke:
    def test_korean_detail_roundtrip(self, store: EventStore) -> None:
        evt = store.record_tool_wrap(
            tool_name="execute_sql",
            args={"query": "DROP TABLE users;"},
            context=None,
            verdict="allow",
        )
        store.record_outcome(
            evt,
            status="error",
            severity=SEVERITY_CRITICAL,
            side_effect="table_dropped",
            detail="DROP TABLE users during content_editor task",
        )
        store.close()
        lines = _read_lines(store._path)
        assert lines[1]["outcome"]["detail"] == "DROP TABLE users during content_editor task"
        assert lines[1]["outcome"]["severity"] == SEVERITY_CRITICAL
        assert lines[1]["outcome"]["side_effect"] == "table_dropped"

    def test_parent_dir_auto_created(self, tmp_path: Path) -> None:
        """부모 디렉터리가 없어도 자동 생성."""
        nested = tmp_path / "a" / "b" / "c" / "events.jsonl"
        s = EventStore(nested)
        s.record_tool_wrap(tool_name="x", args={}, context=None, verdict="allow")
        s.close()
        assert nested.exists()

    def test_each_line_is_valid_json(self, store: EventStore) -> None:
        """한 줄 = 한 JSON 객체 보장."""
        store.record_tool_wrap(tool_name="x", args={}, context=None, verdict="allow")
        evt = store.record_tool_wrap(tool_name="y", args={}, context=None, verdict="allow")
        store.record_outcome(evt, status="ok", severity=SEVERITY_INFO, detail="ok")
        store.close()
        with store._path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    json.loads(line)  # 예외 없으면 통과

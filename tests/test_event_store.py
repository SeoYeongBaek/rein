"""EventStore 회귀 테스트.

잠그는 피드백:
    1. 첫 record_* 동시 호출 race (스레드 안전)
        - record_tool_wrap / record_model_client / record_outcome 모두
          self._lock 안에서 _fh == None 체크 + 파일 open + write를
          한 번에 수행해야 한다. 두 스레드가 동시에 첫 record_*를
          시작해도 파일이 한 번만 열려야 하고, 모든 이벤트가 손실
          없이 기록되어야 한다.
        - record_*가 락 밖에서 _open()을 호출하면 두 번 open race가
          생긴다. 본 테스트는 그 회귀를 막는다.

    2. b안 evt ID 단일화 (D/D.review)
        - self._seq는 단일 카운터로, tool_wrap / model_client / outcome
          모든 라인이 evt_NNNN을 고유하게 발급받아야 한다.
        - model_client는 self._seq += 1로 evt 고유성을 확보하되
          seq 필드에는 null을 박는다 (§9 준수).
        - outcome은 직전 tool_wrap의 evt/seq를 재사용한다.
        - 시간순: tool_wrap → model_client → tool_wrap → outcome 시
          evt ID는 evt_0001, evt_0002, evt_0003, evt_0003이고
          seq 필드는 1, null, 3, 3.
        - 회귀 가드: model_client가 self._seq를 증가시키지 않으면
          evt_0001 중복 + evt_0002 누락이 발생한다 (폐기된 Y안 버그).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from rein.events import EventStore

# === 픽스처 ===


@pytest.fixture
def tmp_store(tmp_path: Path) -> EventStore:
    """각 테스트마다 격리된 임시 경로의 EventStore를 만든다."""
    return EventStore(tmp_path / "events.jsonl")


def _read_jsonl(path: Path) -> list[dict]:
    """JSONL 한 줄씩 읽어서 list[dict]로 반환. 빈 줄은 무시."""
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# === 피드백 1: 스레드 안전 race ===


class TestThreadSafety:
    """record_* 동시 호출 시 _open race 없음 + 모든 이벤트 손실 없음."""

    def test_concurrent_first_record_opens_file_once(self, tmp_path: Path) -> None:
        """두 스레드가 동시에 첫 record_tool_wrap를 호출해도
        파일이 한 번만 열리고 모든 이벤트가 손실 없이 기록된다."""
        path = tmp_path / "concurrent_first.jsonl"
        store = EventStore(path)
        results: list[dict] = []

        def worker(i: int) -> None:
            evt = store.record_tool_wrap(
                tool_name=f"tool_{i}",
                args={"i": i},
                context=None,
                verdict="allow",
            )
            results.append(evt)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # (1) 파일이 정확히 20줄 (모든 이벤트 손실 없이 기록됨)
        events = _read_jsonl(path)
        assert len(events) == 20

        # (2) seq가 1..20 단조 증가 (단일 카운터 보존)
        seqs = sorted(e["seq"] for e in events)
        assert seqs == list(range(1, 21))

        # (3) evt ID가 evt_0001..evt_0020으로 모두 고유
        evts = sorted(e["evt"] for e in events)
        assert evts == [f"evt_{i:04d}" for i in range(1, 21)]

    def test_concurrent_mixed_records_no_loss(self, tmp_path: Path) -> None:
        """record_tool_wrap / record_model_client / record_outcome을
        섞어서 동시 호출해도 모든 라인이 손실 없이 기록되고
        evt ID가 항상 고유해야 한다."""
        path = tmp_path / "concurrent_mixed.jsonl"
        store = EventStore(path)
        tool_wrap_events: list[dict] = []

        def worker_tw(i: int) -> None:
            evt = store.record_tool_wrap(
                tool_name=f"tool_{i}",
                args={"i": i},
                context=None,
                verdict="allow",
            )
            tool_wrap_events.append((i, evt))

        def worker_mc(i: int) -> None:
            store.record_model_client(
                parent_seq=None,
                tool_name=f"tool_{i}",
                proposed_args={"i": i},
            )

        threads: list[threading.Thread] = []
        # tool_wrap 10개, model_client 10개 섞어서 동시 실행
        for i in range(10):
            threads.append(threading.Thread(target=worker_tw, args=(i,)))
            threads.append(threading.Thread(target=worker_mc, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = _read_jsonl(path)
        # 20줄 모두 기록됨 (tool_wrap 10 + model_client 10)
        assert len(events) == 20

        # evt ID 모두 고유 (race로 인한 중복/누락 없음)
        evts = [e["evt"] for e in events]
        assert len(set(evts)) == 20

        # tool_wrap 라인의 seq는 1..10 범위 (매칭 키 단일 카운터)
        tw_events = [e for e in events if e["source"] == "tool_wrap"]
        tw_seqs = sorted(e["seq"] for e in tw_events)
        assert tw_seqs == list(range(1, 11))

        # model_client 라인의 seq 필드는 모두 null (§9)
        mc_events = [e for e in events if e["source"] == "model_client"]
        assert len(mc_events) == 10
        assert all(e["seq"] is None for e in mc_events)


# === 피드백 2: b안 evt ID 단일화 ===


class TestEvtIdSingleCounter:
    """evt ID 단일 카운터 약속 (D/D.review)."""

    def test_tool_wrap_evt_id_increments(self, tmp_store: EventStore) -> None:
        """tool_wrap 연속 호출: evt ID가 evt_0001, evt_0002, ...로 증가."""
        e1 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        e2 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        e3 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")

        assert e1["evt"] == "evt_0001"
        assert e2["evt"] == "evt_0002"
        assert e3["evt"] == "evt_0003"
        assert e1["seq"] == 1
        assert e2["seq"] == 2
        assert e3["seq"] == 3

    def test_model_client_evt_id_unique_with_tool_wrap(self, tmp_store: EventStore) -> None:
        """[D/D.review 핵심 회귀 가드]

        시간순: tool_wrap → model_client → tool_wrap
        - γ-1 (현재): self._seq는 tool_wrap 매칭 키 (1..N 단조).
        record_model_client는 self._seq를 건드리지 않고 self._evt_seq만
        +1한다. 결과:
            · tool_wrap 1: seq=1, evt=evt_0001
            · model_client: seq=null, evt=evt_0002, parent_seq=1
            · tool_wrap 2: seq=2 (← 1..N 단조 보존), evt=evt_0003
        - 회귀 가드:
            · 폐기된 Y안(model_client가 _seq 안 증가 + 단일 _seq)이 다시
            들어오면 tool_wrap의 evt가 중복되거나 evt_0002가 누락되어
            이 테스트가 실패한다.
            · 폐기된 b안(model_client도 _seq 증가)이 다시 들어오면
            e3["seq"]가 3이 되어 단조 가정이 깨지므로 이 테스트가 실패한다.
        """
        e1 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        e2 = tmp_store.record_model_client(parent_seq=1, tool_name="t", proposed_args={})
        e3 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")

        # evt ID 모두 고유 (γ-1: self._evt_seq 단일 카운터)
        assert e1["evt"] == "evt_0001"
        assert e2["evt"] == "evt_0002"
        assert e3["evt"] == "evt_0003"
        assert len({e1["evt"], e2["evt"], e3["evt"]}) == 3

        # §6 매칭 키: tool_wrap seq는 1..N 단조 (γ-1: model_client가
        # self._seq를 건드리지 않으므로 깨끗)
        assert e1["seq"] == 1
        assert e2["seq"] is None  # §9 "model_client seq 미부여"
        assert e3["seq"] == 2  # γ-1: tool_wrap 매칭 키 단조

        # model_client의 parent_seq는 선행 tool_wrap 표면화
        assert e2["parent_seq"] == 1

    def test_outcome_reuses_tool_wrap_evt_and_seq(self, tmp_store: EventStore) -> None:
        """outcome 라인은 tool_wrap 라인과 evt/seq를 공유한다.
        self._seq도 증가시키지 않음 ([D])."""
        e1 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")

        # outcome 직전에 tool_wrap 한 번 더 호출 → seq=2
        e2 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")

        tmp_store.record_outcome(
            e2,
            status="ok",
            severity="info",
            side_effect=None,
            detail="done",
        )

        # 마지막 두 tool_wrap 사이 outcome이 끼면 evt ID는:
        # e1=evt_0001, e2=evt_0002, outcome=evt_0002 (재사용)
        assert e1["evt"] == "evt_0001"
        assert e2["evt"] == "evt_0002"
        # outcome 라인의 evt/seq는 e2와 동일
        events = _read_jsonl(tmp_store._path)
        outcome_line = [ev for ev in events if ev["source"] == "outcome"]
        assert len(outcome_line) == 1
        assert outcome_line[0]["evt"] == "evt_0002"
        assert outcome_line[0]["seq"] == 2

    def test_full_sequence_no_gaps_or_duplicates(self, tmp_store: EventStore) -> None:
        """tool_wrap / model_client / outcome 혼합 시나리오에서
        evt ID에 누락/중복이 없어야 한다."""
        # tool_wrap 1
        tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        # model_client (tool_wrap 1에 선행)
        tmp_store.record_model_client(parent_seq=1, tool_name="t", proposed_args={})
        # tool_wrap 2
        e2 = tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        # outcome (tool_wrap 2의 결과)
        tmp_store.record_outcome(e2, status="ok", severity="info", detail="ok")
        # model_client (outcome 이후 — 다음 tool_wrap은 없음)
        tmp_store.record_model_client(parent_seq=2, tool_name="t", proposed_args={})

        events = _read_jsonl(tmp_store._path)

        # 모든 evt ID 추출 (도구 호출 순서 무관하게 정렬)
        evts = sorted(e["evt"] for e in events)

        # 기대값: tool_wrap 1, model_client 1, tool_wrap 2, outcome 2,
        #        model_client 2 — 총 5개의 고유 evt ID
        # tool_wrap 2와 outcome은 같은 evt 공유 (evt_0003)
        # model_client는 자기 카운터 슬롯을 차지함 (evt_0002, evt_0005)
        assert evts == [
            "evt_0001",  # tool_wrap 1
            "evt_0002",  # model_client (tool_wrap 1 선행)
            "evt_0003",  # tool_wrap 2
            "evt_0003",  # outcome (tool_wrap 2 공유)
            "evt_0004",  # model_client (tool_wrap 2 선행)
        ]

        # evt 중복은 정확히 outcome 라인에서만 허용 (tool_wrap와 공유)
        from collections import Counter

        counts = Counter(e["evt"] for e in events)
        # evt_0003만 2회, 나머지는 1회
        assert counts["evt_0003"] == 2
        for k, v in counts.items():
            if k != "evt_0003":
                assert v == 1


# === 추가 회귀 가드: §6 매칭 키가 model_client 카운터를 차지해도 안전 ===


class TestReplayMatchingUnaffected:
    """§6 매칭 키는 source=tool_wrap 라인의 seq만 사용.
    model_client가 self._seq를 차지해도 매칭에 영향 없음을 검증."""

    def test_matching_key_skips_model_client(self, tmp_store: EventStore) -> None:
        """리플레이 매칭 관점에서 tool_wrap seq는 1..N 단조 증가.
        model_client가 그 사이에 끼면 seq 필드값은 단조 증가하지만
        source=tool_wrap 라인의 seq만 매칭 키로 쓰면 1..N이 깨끗.
        """
        # tool_wrap 1
        tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")
        # model_client 끼움 (seq 필드는 null)
        tmp_store.record_model_client(parent_seq=1, tool_name="t", proposed_args={})
        # tool_wrap 2
        tmp_store.record_tool_wrap(tool_name="t", args={}, context=None, verdict="allow")

        events = _read_jsonl(tmp_store._path)

        # source=tool_wrap 라인만 추출
        tw_events = [e for e in events if e["source"] == "tool_wrap"]
        tw_seqs = [e["seq"] for e in tw_events]

        # tool_wrap 라인의 seq는 1, 2 — 매칭 키로 깨끗
        assert tw_seqs == [1, 2]

        # model_client 라인은 seq=null — 매칭 대상에서 제외됨
        mc_events = [e for e in events if e["source"] == "model_client"]
        assert all(e["seq"] is None for e in mc_events)

"""이벤트 저장소 (CLAUDE.md §6, §9). append-only JSONL, 단일 순번 카운터.

설계 결정 (구현자 메모):
    [D1] 파일 수명: lazy open. 호출 시점에 열린다. 닫는 건 close()로
         명시적으로만 — Harness __exit__ 책임. 별도 public API는 두지
         않는다 (scope guard §4 "5줄 통합" 정신).
    [D2] seq 카운터: EventStore 인스턴스 필드. tool_wrap 기록마다 +1.
         model_client 이벤트는 seq 미부여 (§9 확정, §6 매칭 키 제외).
    [D3] evt ID (tool_wrap): "evt_" + seq zero-pad 4자리.
    [D]  evt ID (model_client): §9에 명시된 규칙이 없어, tool_wrap evt
         ID("evt_NNNN")와 충돌하지 않게 "evt_mc_NNNN" 별도 카운터 +
         prefix를 임시 채택. 후속 PR(replay engine)에서 model_client
         식별 규칙이 확정되면 거기서 정렬.
    [D4] outcome: 호출자가 status/severity/side_effect/detail을 명시.
         성공 시 record_ok(), 예외 시 record_error(exc) 헬퍼 제공.
         §7 분류 테이블 세부 severity refine은 후속 PR.
    [D5] Context: 본 PR에서는 best-effort 직렬화 (None→{}, dict→dict,
         객체→__dict__). 의미 있는 필드 채움은 후속 PR.

public 표면 노출 정책:
    - EventStore: 공개 (§4 5줄 통합의 디테일)
    - SEVERITY_*: 공개 (rule YAML 작성자가 severity enum 값을 import할 길)
    - SCHEMA_VERSION: 비공개. 내부 직렬화 디테일 — 사용자가 직접 import할
      이유가 없음. 노출이 필요해지면 그때 별도 PR로.
"""

from rein.events.event_store import (
    SCHEMA_VERSION,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    EventStore,
)

__all__ = [
    "EventStore",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "SCHEMA_VERSION",
]

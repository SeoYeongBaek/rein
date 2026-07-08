"""EventStore 구현 본체. 모듈 docstring은 rein/storage/__init__.py 참조.

[A] append-only, 단일 순번 seq : _write_line이 항상 append + flush.
    self._seq는 tool_wrap 매칭 키 전용 카운터다 (§6). tool_wrap
    이벤트에 부여되는 seq 필드값과 1:1 대응하며, 호출마다 +1.
    model_client / outcome은 self._seq를 건드리지 않는다 — §6 매칭
    키가 source=tool_wrap 라인만 사용되고 그 라인의 seq가 1..N
    단조 증가해야 하기 때문. §9 "단일 순번 카운터" 약속도 여기서
    만족된다 (model_client의 seq 필드는 null).
[B] schema_version 포함 : 모든 record_*가 "schema_version": SCHEMA_VERSION 박음
[C] outcome.detail 자유 텍스트 : record_outcome(detail=...),
    record_ok/record_error 모두 detail 받음, 기본값은 예외 타입+메시지
[D] evt ID 단일 카운터 (= self._evt_seq) : tool_wrap / model_client /
    outcome 모든 라인이 동일한 self._evt_seq에서 evt_NNNN을 발급받는다.
    따라서 evt 필드는 항상 고유한 이벤트 식별자다. CLI 인터페이스
    (예: rein rule-from --event evt_0042)에서 evt ID 하나로 정확히
    한 라인을 참조할 수 있다.
    ※ 두 카운터의 책임 분리 (코드와 1:1 일치):
        · self._seq — tool_wrap 매칭 키 (§6) + §9 "단일 순번 카운터"
                      ⇒ tool_wrap 이벤트에만 +1 증가한다.
                        model_client / outcome 이벤트는 이 카운터를
                        건드리지 않는다 (= 증가시키지 않는다).
        · self._evt_seq — 모든 evt ID 발급 (D)
                      ⇒ tool_wrap / model_client 이벤트가 +1 증가한다.
                        outcome 이벤트는 직전 tool_wrap의 evt_id를
                        그대로 재사용하므로 증가시키지 않는다.
    즉, "record_*가 어떤 카운터를 증가시키는지"는 함수별로 완전히
    고정돼 있다:
        · record_tool_wrap  : self._seq += 1, self._evt_seq += 1
        · record_model_client: self._evt_seq += 1 (self._seq는 증가 X)
        · record_outcome    : 둘 다 증가 X (직전 tool_wrap의 evt/seq 재사용)

[E] 스레드 안전: 파일 open + seq 증가 + write를 record_* 호출마다 단일
    self._lock 획득 안에서 원자적으로 수행한다. record_* 진입 시점에
    _fh가 None이면 락 안에서 직접 파일을 연다. 이는 _open() private
    진입점을 락 밖에서 호출할 때 발생하는 두 번 open race를 막기 위한
    결정이다. _open()은 Harness.__enter__처럼 락 컨텍스트를 외부에서
    보장하는 단일 진입점 전용으로 유지한다.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, TextIO

# §9 스키마 고정 enum
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

SOURCE_TOOL_WRAP = "tool_wrap"
SOURCE_MODEL_CLIENT = "model_client"
SOURCE_OUTCOME = "outcome"

# §9 스키마 버전. 모듈 내부 상수 — 외부 노출 표면 아님.
# 본 버전은 §8 rules.yaml의 feature_schema와 대칭축을 이루며, §9
# 확정값 "v1"에서 시작한다. 후속 버전에서 severity enum 등 확장 시
# 옛 로그와의 호환을 본 상수가 관리한다.
SCHEMA_VERSION = "v1"  # internal


class EventStore:
    """Append-only JSONL 저장소. thread-safe.

    Args:
        path: JSONL 파일 경로. 부모 디렉터리는 자동 생성한다.

    Notes:
        - 파일은 첫 record_* 호출 시점에 열린다 ([D1] lazy open).
          단, record_* 진입 경로에서는 락 안에서 직접 열고, _open()은
          Harness.__enter__처럼 외부에서 락 컨텍스트를 보장하는
          진입점에서만 호출된다.
        - 두 카운터의 책임 분리 (코드와 1:1 일치):
            · self._seq — tool_wrap 매칭 키 + §9 단일 순번 카운터.
              ⇒ tool_wrap 이벤트에서만 +1 증가.
                model_client / outcome 이벤트는 이 카운터를
                건드리지 않는다 (= 증가 X).
            · self._evt_seq — 모든 evt ID 발급 (tool_wrap + model_client).
              ⇒ tool_wrap / model_client에서 +1 증가.
                outcome은 직전 tool_wrap의 evt_id를 그대로 재사용
                (증가 X).
        - §6 리플레이 매칭 키는 source=tool_wrap 라인의 seq만 사용.
          tool_wrap seq는 1..N 단조 (§6 매칭 깨끗).
        - outcome은 동일 이벤트에 대한 별도 라인으로 적재 (append-only 유지).
        - _open / _close는 private 진입점 (§4 "5줄 통합" 정신).
          외부에서 닫을 필요가 생기면 공개 close()를 쓸 것 (§14 —
          "Harness 외의 다른 곳에서도 안전하게 닫을 수 있도록").
        - 공개 close()는 idempotent하고 self._lock 안에서 동작한다.
          이미 닫힌 상태에서 호출해도 안전하다.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fh: TextIO | None = None  # [D1] lazy open
        self._seq: int = 0  # [A] tool_wrap 매칭 키 + §9 단일 순번 카운터
        self._evt_seq: int = 0  # [D] 모든 evt ID 발급 (단일 카운터)
        self._lock = threading.Lock()

    # ---- 수명 관리 ----

    def _open(self) -> None:
        """파일을 append 모드로 연다. 이미 열려있으면 idempotent.

        private 진입점 (§4 "5줄 통합" 정신). 외부에서 호출하는 경로는
        Harness.__enter__ 같이 락 컨텍스트를 외부에서 보장하는 단일
        진입점으로 한정된다. record_* 경로에선 호출 금지 — record_*
        는 락 안에서 직접 파일을 연다 ([E]).
        """
        if self._fh is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def _close(self) -> None:
        """핸들을 flush + close.

        private 진입점 (§4 "5줄 통합" 정신). 외부 호출자는
        Harness.__exit__ 또는 공개 close()로 한정된다.
        """
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def close(self) -> None:
        """핸들을 flush + close 한다. 공개 진입점.

        이미 닫혀 있어도 안전(idempotent). 스레드 안전을 위해
        self._lock 안에서 _close를 수행한다. Harness.__exit__가
        외부에서 락을 이미 잡고 있을 수 있으므로 — 그러면 여기서
        같은 락을 다시 잡으려는 시도가 발생할 수 있는데 — threading
        .Lock은 같은 스레드에서 재진입이 불가능하므로, __exit__는
        self._lock 없이 직접 _close를 호출하고, 그 외 외부 호출자는
        본 메서드를 통해서만 닫게 한다(역할 분리).

        Returns:
            None
        """
        with self._lock:
            self._close()

    # ---- tool_wrap 이벤트 ([A][B][D]) ----

    def record_tool_wrap(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        context: Any,
        verdict: str,
    ) -> dict[str, Any]:
        """tool_wrap 이벤트를 한 줄 JSON으로 append하고 직렬화된 이벤트를
        돌려준다.

        카운터 동작 (코드와 1:1 일치):
            · self._seq += 1     (tool_wrap 매칭 키, §6)
            · self._evt_seq += 1 (evt ID 발급, [D])

        [B] schema_version 박음.
        [E] 락 안에서 open + 두 카운터 증가 + write를 원자적으로 수행.
        """
        with self._lock:
            # [D1][E] open 로직을 락 안에서 직접.
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            assert self._fh is not None

            self._seq += 1  # [A] tool_wrap 매칭 키. 본 함수에서만 +1.
            self._evt_seq += 1  # [D] evt ID 발급.
            seq = self._seq
            evt_id = self._make_evt_id(self._evt_seq)

            event = {
                "schema_version": SCHEMA_VERSION,  # [B]
                "evt": evt_id,
                "seq": seq,  # [A] §6 매칭 키 + §9 단일 순번 카운터
                "source": SOURCE_TOOL_WRAP,
                "parent_seq": None,
                "tool_name": tool_name,
                "args": args,
                "context": _serialize_context(context),  # [D5]
                "verdict": verdict,
            }

            self._write_line(event)
            return event

    # ---- model_client 이벤트 ([D]) ----

    def record_model_client(
        self,
        *,
        parent_seq: int | None,
        tool_name: str | None,
        proposed_args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """model_client 이벤트를 적재한다. §9에 따라 seq 필드값은 null.

        카운터 동작 (코드와 1:1 일치):
            · self._seq += 0     (증가시키지 않음. §6 매칭 키 보존 —
                                  다음 tool_wrap이 seq=N+1을 받아
                                  tool_wrap 라인의 seq가 1..N 단조로
                                  깨끗해야 하므로 본 함수는 self._seq를
                                  건드리지 않는다)
            · self._evt_seq += 1 (evt ID 발급, [D])

        [E] 락 안에서 open + self._evt_seq 증가 + write를 원자 수행.

        Args:
            parent_seq: 이 모델 제안이 선행하는 tool_wrap의 seq. 타임라인
                렌더링 전용 (§6)이며 리플레이 매칭에는 사용되지 않는다.
            tool_name: LLM이 제안한 tool_use의 도구명 (없으면 None).
            proposed_args: LLM이 제안한 인자 (없으면 None).

        Returns:
            직렬화된 이벤트 dict. evt_id는 "evt_NNNN" 형식 (단일 카운터
            발급, 다른 라인과 항상 고유).
        """
        with self._lock:
            # [D1][E] open 로직을 락 안에서 직접.
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            assert self._fh is not None

            # [D] evt ID 발급을 위해 self._evt_seq만 +1.
            # self._seq는 건드리지 않는다 (= 증가 X) — §6 매칭 키
            # (tool_wrap seq 1..N 단조) 보존이 본 함수의 핵심 불변식.
            self._evt_seq += 1
            # self._seq는 의도적으로 그대로 둔다. 증가시키지 않는다.
            evt_id = self._make_evt_id(self._evt_seq)

            event = {
                "schema_version": SCHEMA_VERSION,  # [B]
                "evt": evt_id,
                "seq": None,  # [D] §9 "model_client seq 미부여" (필드값)
                "source": SOURCE_MODEL_CLIENT,
                "parent_seq": parent_seq,  # [D] 선행 tool_wrap seq
                "tool_name": tool_name,
                "args": proposed_args if proposed_args is not None else {},
                "context": {},
                "verdict": None,
            }

            self._write_line(event)
            return event

    # ---- outcome ([C]) ----

    def record_outcome(
        self,
        event: dict[str, Any],
        *,
        status: str,
        severity: str,
        side_effect: str | None = None,
        detail: str | None = None,  # [C] 자유 텍스트
    ) -> None:
        """이미 적재된 tool_wrap 이벤트에 대한 outcome 라인을 별도 append.

        카운터 동작 (코드와 1:1 일치):
            · self._seq += 0     (증가 X. 직전 tool_wrap의 seq 재사용)
            · self._evt_seq += 0 (증가 X. 직전 tool_wrap의 evt 재사용)

        [E] 락 안에서 open + write를 원자 수행.

        Args:
            event: record_tool_wrap가 반환한 직전 이벤트.
            status: "ok" | "error" (M1은 둘만; §7 분류 refine은 후속 PR).
            severity: §9 enum ("info" | "warning" | "critical").
            side_effect: 선택. 예: "table_dropped".
            detail: [C] 자유 텍스트.
        """
        with self._lock:
            # [D1][E] open 로직을 락 안에서 직접.
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            assert self._fh is not None

            # outcome은 두 카운터를 모두 증가시키지 않는다.
            # 직전 tool_wrap의 evt / seq를 그대로 재사용.
            outcome_event = {
                "schema_version": SCHEMA_VERSION,  # [B]
                "evt": event["evt"],
                "seq": event["seq"],
                "source": SOURCE_OUTCOME,
                "parent_seq": event["seq"],
                "tool_name": event["tool_name"],
                "outcome": {
                    "status": status,
                    "side_effect": side_effect,
                    "severity": severity,
                    "detail": detail,  # [C]
                },
            }

            self._write_line(outcome_event)

    # ---- outcome 헬퍼 (성공 / 예외) ----

    def record_ok(
        self,
        event: dict[str, Any],
        *,
        side_effect: str | None = None,
        detail: str | None = None,
    ) -> None:
        """do_call 성공 outcome. severity="info" 고정 (M1 한정).

        카운터: 두 카운터 모두 증가 X (record_outcome 경유).
        """
        self.record_outcome(
            event,
            status="ok",
            severity=SEVERITY_INFO,
            side_effect=side_effect,
            detail=detail,
        )

    def record_error(
        self,
        event: dict[str, Any],
        exc: BaseException,
        *,
        severity: str = SEVERITY_WARNING,
        side_effect: str | None = None,
        detail: str | None = None,
    ) -> None:
        """do_call 예외 outcome. detail 기본값은 예외 타입+메시지.

        카운터: 두 카운터 모두 증가 X (record_outcome 경유).
        """
        if detail is None:
            detail = f"{type(exc).__name__}: {exc}"
        self.record_outcome(
            event,
            status="error",
            severity=severity,
            side_effect=side_effect,
            detail=detail,
        )

    # ---- 내부 유틸 ----

    def _make_evt_id(self, seq: int) -> str:
        """[D3][D] evt ID. seq zero-pad 4자리. self._evt_seq 단일
        카운터에서 발급되며 tool_wrap / model_client / outcome 모든
        라인이 evt_NNNN 형식을 공유하고 항상 고유하다. 10000건 초과
        시 format 폭이 자연 확장되며 이때 _make_evt_id 한 줄만 수정."""
        return f"evt_{seq:04d}"

    def _write_line(self, payload: dict[str, Any]) -> None:
        """한 줄 JSON + 개행. 호출자는 self._lock을 잡고 있어야 한다."""
        assert self._fh is not None
        self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._fh.flush()


def _serialize_context(ctx: Any) -> dict[str, Any]:
    """[D5] Context 객체를 dict로 best-effort 직렬화."""
    if ctx is None:
        return {}
    if isinstance(ctx, dict):
        return dict(ctx)
    return dict(getattr(ctx, "__dict__", {}) or {})

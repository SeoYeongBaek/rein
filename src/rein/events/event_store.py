"""EventStore 구현 본체. 모듈 docstring은 rein/storage/__init__.py 참조.

"
[A] append-only, 단일 순번 seq : _write_line이 항상 append + flush,
record_tool_wrap이 self._seq += 1만 보유
[B] schema_version 포함 : 모든 record_*가 "schema_version": SCHEMA_VERSION 박음
[C] outcome.detail 자유 텍스트 : record_outcome(detail=...),
record_ok/record_error 모두 detail 받음, 기본값은 예외 타입+메시지
[D] model_client는 parent_seq만, seq 없음 :
record_model_client의 "seq": None + parent_seq 보유, self._seq 증가 안 함
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
        - 파일은 첫 append 시점에 열린다 ([D1] lazy open).
        - seq는 source=tool_wrap 이벤트에만 부여된다 ([D2]).
        - source=model_client 이벤트는 seq 미부여, parent_seq만 부여 (§9).
        - outcome은 동일 이벤트에 대한 별도 라인으로 적재 (append-only 유지).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fh: TextIO | None = None  # [D1] lazy open
        self._seq: int = 0  # [D2] tool_wrap 전용 단조 증가 카운터
        self._lock = threading.Lock()

    # ---- 수명 관리 ----

    def open(self) -> None:
        """파일을 append 모드로 연다. 이미 열려있으면 idempotent.

        [D1] 호출 시점:
            - 첫 record_*() 호출 시점 (register_tool-only 경로 대응)
            - 또는 Harness.__enter__ 진입 시점
        """
        if self._fh is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def close(self) -> None:
        """핸들을 flush + close. Harness.__exit__ 전용 진입점.

        public 노출은 의도적으로 안 함 — 호출자는 Harness의 컨텍스트
        매니저를 통해 수명을 관리해야 한다 (§4 "5줄 통합" 표면).
        """
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    # ---- tool_wrap 이벤트 ([A][B]) ----

    def record_tool_wrap(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        context: Any,
        verdict: str,
    ) -> dict[str, Any]:
        """tool_wrap 이벤트를 한 줄 JSON으로 append하고 직렬화된 이벤트를
        돌려준다. 호출자는 반환값을 보관해 후속 outcome 라인에 evt/seq를
        연결할 수 있다.

        [A] append-only: 매 호출은 정확히 한 줄을 추가한다. 기존 줄은
            절대 수정하지 않는다.
        [A] 단일 순번 카운터: EventStore 인스턴스가 보유한 self._seq를
            호출마다 1씩 증가시킨다. tool_wrap 이벤트 외 경로는 이
            카운터를 사용하지 않는다.
        [B] schema_version: §9 확정값 "v1"을 매 이벤트에 박는다.
        """
        self.open()
        assert self._fh is not None

        with self._lock:
            self._seq += 1  # [A] tool_wrap에서만 증가
            seq = self._seq
            evt_id = self._make_evt_id(seq)

            event = {
                "schema_version": SCHEMA_VERSION,  # [B]
                "evt": evt_id,
                "seq": seq,
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
        """model_client 이벤트를 적재한다. §9에 따라 seq 미부여.

        [D] source=model_client 이벤트는 seq 없이 parent_seq만 갖는다.
            본 메서드도 tool_wrap 카운터(self._seq)를 증가시키지 않는다
            — 매칭 키에서 원천 제외되는 경로이기 때문 (§6).

        Args:
            parent_seq: 이 모델 제안이 선행하는 tool_wrap의 seq. 타임라인
                렌더링 전용 (§6)이며 리플레이 매칭에는 사용되지 않는다.
            tool_name: LLM이 제안한 tool_use의 도구명 (없으면 None).
            proposed_args: LLM이 제안한 인자 (없으면 None).

        Returns:
            직렬화된 이벤트 dict. evt_id는 부모가 없으면 "evt_mc_NNNN"
            형식 (model_client 전용 0-pad; tool_wrap 카운터와 충돌하지
            않게 별도 진행).
        """
        self.open()
        assert self._fh is not None

        with self._lock:
            # [D] seq 미부여. 별도 카운터를 쓰지 않고 self._seq를 참조만
            # 하지 않는다 — model_client는 순번 체계에서 분리됨.
            evt_id = self._next_model_client_evt_id()

            event = {
                "schema_version": SCHEMA_VERSION,  # [B]
                "evt": evt_id,
                "seq": None,  # [D] 명시적 null
                "source": SOURCE_MODEL_CLIENT,
                "parent_seq": parent_seq,
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

        [A] append-only 보존: 직전 tool_wrap 라인을 다시 쓰지 않고,
            같은 evt/seq를 표면에 실은 outcome 라인을 새 줄로 추가한다.
            리플레이 시점에 같은 evt/seq로 매칭해 머지하는 책임은 후속
            PR의 replay engine에 있다.

        Args:
            event: record_tool_wrap가 반환한 직전 이벤트.
            status: "ok" | "error" (M1은 둘만; §7 분류 refine은 후속 PR).
            severity: §9 enum ("info" | "warning" | "critical").
            side_effect: 선택. 예: "table_dropped".
            detail: [C] 자유 텍스트. severity enum만으로 안 잡히는 구체
                    사유. 리포트가 detail만 읽어도 재조사 없이 사건을
                    파악할 수 있도록 §9 확정.
        """
        self.open()
        assert self._fh is not None

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

        with self._lock:
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

        §7 분류 테이블의 세부 severity는 후속 PR에서 refine.
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

        §7 분류 테이블의 세부 severity refine은 후속 PR.
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
        """[D3] tool_wrap evt ID. seq zero-pad 4자리. 10000건 초과 시
        format 폭이 자연 확장되며 이때 _make_evt_id 한 줄만 수정."""
        return f"evt_{seq:04d}"

    def _next_model_client_evt_id(self) -> str:
        """[D] model_client 전용 evt ID. self._seq와 분리된 카운터로
        "evt_mc_NNNN" 형식. tool_wrap evt ID와 충돌하지 않게 별도 진행."""
        # lazy-init 카운터: model_client 경로가 한 번도 안 불렸으면 0에서 시작.
        if not hasattr(self, "_mc_seq"):
            self._mc_seq = 0
        self._mc_seq += 1
        return f"evt_mc_{self._mc_seq:04d}"

    def _write_line(self, payload: dict[str, Any]) -> None:
        """한 줄 JSON + 개행. 호출자는 self._lock을 잡고 있어야 한다."""
        assert self._fh is not None
        self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._fh.flush()


def _serialize_context(ctx: Any) -> dict[str, Any]:
    """[D5] Context 객체를 dict로 best-effort 직렬화.

    현재 Context는 harness.py에서 비어있는 wrapper이며, 의미 있는 필드
    (task, agent_role 등) 채움은 사용자 측 API가 후속 PR에 확정되기를
    기다린다. 따라서 None→{}, dict→dict 복사, 객체→__dict__ 사본.
    후속 PR에서 typed Context 확정 시 이 함수는 별도 serializer로 교체."""
    if ctx is None:
        return {}
    if isinstance(ctx, dict):
        return dict(ctx)
    return dict(getattr(ctx, "__dict__", {}) or {})

"""이슈 #65: §5 세션 누적 상태와 §9 로그 정적 메타데이터 분리 검증.

#64 결정 (b)를 harness.py에 구현. register_tool wrapper가
    · stage 함수 — Harness 내부 self._session_state (세션 누적 dict)
    · record_tool_wrap — self._context의 호출 시점 얕은 복사본
을 서로 다른 객체로 받는다. 사용자 공개 API(Harness(context=dict|None))
시그니처는 변경 없음.

본 테스트가 검증하는 3개 invariant:

1. 사용자 dict 불변 — stage가 ctx를 mutate해도 Harness에 넘긴 원본
   dict는 보존된다 (사용자 의도 메타데이터 보호).

2. 로그 context 격리 — 각 tool_wrap 이벤트의 context 필드는 호출
   시점의 사용자 입력 그대로, 누적 누출이 0건이다 (§9 "정적 메타데이터"
   약속). N번째 호출의 로그가 (N+1)번째 호출의 stage mutation 결과로
   오염되지 않는다.

3. session state 누적 — session state는 Harness 생애주기 동안
   유지되어 stage가 호출 간 카운터/누적 값을 읽고 쓸 수 있다.
   budget stage PR(§12 M4)이 의존할 contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rein.guardrails.verdict import Verdict
from rein.harness import Harness

STAGE_ORDER: list[str] = ["counter_stage"]


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _make_counter_stage(observed: list[dict[str, Any]]):
    """session state에 counter를 누적하는 stage.

    observed 리스트에 매 호출 시점의 ctx 얕은 복사를 남긴다 — 검증용.
    """

    def stage(tool_call: dict[str, Any], ctx: Any) -> tuple[Verdict, str, str, str]:
        # 검증용으로 mutate 시점 ctx의 사본 저장
        observed.append(dict(ctx) if isinstance(ctx, dict) else {})
        # session state mutation (budget stage가 이렇게 누적할 것)
        ctx["counter"] = ctx.get("counter", 0) + 1
        ctx["last_tool"] = tool_call["name"]
        return Verdict.ALLOW, "", "", ""

    return stage


# === invariant 1: 사용자 dict 불변 ===


def test_user_context_dict_not_mutated_by_stage(tmp_path: Path) -> None:
    """사용자가 Harness에 넘긴 context dict 자체는 stage가 mutate해도 보존된다.

    #64 결정: stage가 ctx를 mutate해도 사용자 원본 dict는 손상되지 않는다.
    같은 dict를 다른 agent_role용으로 재사용해도 안전.
    """
    record_path = tmp_path / "run.jsonl"
    user_ctx: dict[str, Any] = {
        "agent_role": "content_editor",
        "task": "공지사항 업데이트",
    }
    observed: list[dict[str, Any]] = []

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(record=record_path, context=user_ctx)
        h.register_stage("counter_stage", _make_counter_stage(observed))

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")

    # stage가 session state를 mutate했지만 사용자 dict는 그대로
    assert user_ctx == {
        "agent_role": "content_editor",
        "task": "공지사항 업데이트",
    }
    assert "counter" not in user_ctx
    assert "last_tool" not in user_ctx


# === invariant 2: 로그 context 격리 ===


def test_log_context_isolated_per_call(tmp_path: Path) -> None:
    """각 호출의 tool_wrap context는 호출 시점 snapshot. 누적 누출 0건.

    N번째 호출의 stage가 session state에 counter=N을 남겨도, N번째
    tool_wrap의 context 필드는 사용자 입력 그대로여야 한다 (§9 약속).
    """
    record_path = tmp_path / "run.jsonl"
    observed: list[dict[str, Any]] = []

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(record=record_path, context={"agent_role": "content_editor"})
        h.register_stage("counter_stage", _make_counter_stage(observed))

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")
        execute_sql(query="SELECT 2;")
        execute_sql(query="SELECT 3;")

    events = _read_events(record_path)
    tool_wraps = [e for e in events if e["source"] == "tool_wrap"]

    assert len(tool_wraps) == 3

    # 세 호출 모두 로그 context는 사용자 입력 그대로 (§9 정적 메타데이터)
    for tw in tool_wraps:
        assert tw["context"] == {"agent_role": "content_editor"}, (
            f"§9 위반: 호출 시점 외 mutation이 로그에 새어들었다. "
            f"context={tw['context']!r}"
        )
        # session state 흔적 0건
        assert "counter" not in tw["context"]
        assert "last_tool" not in tw["context"]


# === invariant 0: stage가 user context 메타데이터를 읽을 수 있다 (follow-up) ===


def test_stage_can_read_user_context_metadata(tmp_path: Path) -> None:
    """stage 함수가 user context 메타데이터(agent_role 등)를 ctx로 읽을 수 있다.

    [이슈 #65 follow-up] §5/§9 분리 후에도 stage가 정적 메타데이터에
    접근할 수 있어야 한다. 그렇지 않으면 M2 budget stage가 role별
    예산을 적용할 수 없고, stage가 §5 "state는 시그니처에 드러난
    의존성" 원칙을 우회해 self._context를 직접 참조하게 된다.

    contract: self._session_state는 __init__에서 self._context의 얕은
    복사본으로 seed되어, stage의 첫 호출 ctx는 사용자 입력 메타데이터를
    그대로 포함한다.
    """
    record_path = tmp_path / "run.jsonl"
    observed_first_call: list[dict[str, Any]] = []

    def observe_stage(tool_call: dict[str, Any], ctx: Any) -> tuple[Verdict, str, str, str]:
        observed_first_call.append(dict(ctx) if isinstance(ctx, dict) else {})
        return Verdict.ALLOW, "", "", ""

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(
            record=record_path,
            context={
                "agent_role": "content_editor",
                "task": "공지사항 업데이트",
            },
        )
        h.register_stage("counter_stage", observe_stage)

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")

    # stage의 ctx가 사용자 입력 메타데이터를 그대로 포함 (§5 contract)
    assert observed_first_call[0].get("agent_role") == "content_editor"
    assert observed_first_call[0].get("task") == "공지사항 업데이트"


# === invariant 3: session state 누적 ===


def test_session_state_persists_across_calls(tmp_path: Path) -> None:
    """session state는 Harness 생애주기 동안 유지. budget stage PR 대비 contract.

    stage가 session state에 누적한 counter가 다음 호출에서 읽혀야 한다.
    없으면 budget stage(§5)가 호출 간 토큰/시간 누적 자체를 못 한다.
    """
    record_path = tmp_path / "run.jsonl"
    observed: list[dict[str, Any]] = []

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(record=record_path, context={"agent_role": "content_editor"})
        h.register_stage("counter_stage", _make_counter_stage(observed))

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")
        execute_sql(query="SELECT 2;")
        execute_sql(query="SELECT 3;")

    # stage가 매 호출에서 본 ctx의 counter는 0, 1, 2 (누적됨)
    observed_counters = [ctx.get("counter", 0) for ctx in observed]
    assert observed_counters == [0, 1, 2], (
        f"§5 session state 누적이 깨졌다. observed={observed!r}"
    )

    # last_tool은 직전 호출까지 누적된 값
    assert observed[0].get("last_tool") is None
    assert observed[1].get("last_tool") == "execute_sql"
    assert observed[2].get("last_tool") == "execute_sql"


# === context 미지정 경로: 기존 동작 유지 (회귀 가드) ===


def test_no_context_keeps_existing_behavior(tmp_path: Path) -> None:
    """context 미지정 시 로그 context는 빈 dict, session state는 정상 동작.

    #63 + #55 회귀 가드: context= 없이 만든 Harness는 기존처럼 빈 context
    가 기록되고 (§63 user contract), session state만 정상 동작한다.
    """
    record_path = tmp_path / "run.jsonl"
    observed: list[dict[str, Any]] = []

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(record=record_path)  # context 생략
        h.register_stage("counter_stage", _make_counter_stage(observed))

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")
        execute_sql(query="SELECT 2;")

    events = _read_events(record_path)
    tool_wraps = [e for e in events if e["source"] == "tool_wrap"]

    # 로그 context는 빈 dict (기존 동작 그대로)
    for tw in tool_wraps:
        assert tw["context"] == {}

    # session state는 정상 누적 (budget stage contract)
    assert [ctx.get("counter", 0) for ctx in observed] == [0, 1]


# === stage_ctx와 log_ctx가 서로 다른 객체라는 직접 검증 ===


def test_stage_ctx_and_log_ctx_are_different_objects(tmp_path: Path) -> None:
    """stage가 받은 ctx와 log로 들어간 ctx는 서로 다른 객체다 (메모리 동일성).

    같은 id()를 공유하면 분리 invariant가 깨진 것 — §9 보호가 작동하지
    않는 상태. (얕은 복사가 안 됐거나 stage_ctx와 log_ctx가 같은 참조인
    경우를 잡는다.)

    설계: stage_ctx는 self._session_state(생성 시 빈 dict, 호출 간 누적),
    log_ctx는 self._context의 호출 시점 얕은 복사. 두 객체의 "내용"은
    설계상 다르다 — stage_ctx는 session state, log_ctx는 user context.
    검증할 invariant는 "두 객체가 메모리상 다른 참조인가" (id() 비교).
    """
    record_path = tmp_path / "run.jsonl"
    captured_stage_ctx_id: list[int] = []

    def capture_stage(tool_call: dict[str, Any], ctx: Any) -> tuple[Verdict, str, str, str]:
        captured_stage_ctx_id.append(id(ctx))
        return Verdict.ALLOW, "", "", ""

    with (
        patch("rein.harness.load_stage_order", return_value=STAGE_ORDER),
        patch("rein.harness.resolve_stage_order", return_value=STAGE_ORDER),
    ):
        h = Harness(
            record=record_path,
            context={"agent_role": "content_editor"},
        )
        h.register_stage("counter_stage", capture_stage)

        @h.register_tool
        def execute_sql(query: str) -> dict[str, str]:
            return {"status": "ok", "query": query}

        execute_sql(query="SELECT 1;")

    events = _read_events(record_path)
    tool_wrap = next(e for e in events if e["source"] == "tool_wrap")

    log_ctx = tool_wrap["context"]

    # 메모리 동일성: stage_ctx의 id vs log_ctx의 id. 둘이 다르면 분리 OK.
    # 같으면 wrapper가 두 경로에 같은 참조를 넘긴 것 — §9 분리 위반.
    assert captured_stage_ctx_id[0] != id(log_ctx), (
        "§9 분리 위반: stage_ctx와 log_ctx가 같은 객체(id 동일)다. "
        "얕은 복사가 안 됐거나 wrapper에서 같은 참조를 전달."
    )
    # log_ctx는 사용자 의도 그대로
    assert log_ctx == {"agent_role": "content_editor"}

"""M4 #78: register_tool 비동기(async def) 도구 지원 테스트.

신규 의존성(pytest-asyncio) 추가 없이 asyncio.run()으로 감싼 일반
def test_* 함수로 작성한다(설계 스펙
docs/superpowers/specs/2026-08-21-async-tool-support-design.md 참고).
"""

import asyncio
import json

import pytest

from rein.harness import Harness


def test_harness_construct_without_event_loop(tmp_path):
    """이벤트 루프 밖에서 생성해도 asyncio.Lock() eager 생성이 에러 없이 통과하는지 확인"""
    h = Harness(record=tmp_path / "run.jsonl")
    assert h._async_lock is not None
    assert h._in_flight_tool is None


def test_async_tool_basic_call(tmp_path):
    """async 도구 등록·호출이 정상 동작하고 tool_wrap+outcome이 기록되는지 확인"""
    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def add(a: int, b: int) -> int:
        return a + b

    result = asyncio.run(add(2, 3))
    assert result == 5

    h._event_store.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["source"] == "tool_wrap"
    assert lines[0]["verdict"] == "allow"
    assert lines[1]["source"] == "outcome"
    assert lines[1]["outcome"]["status"] == "ok"


def test_async_tool_exception_records_error(tmp_path):
    """도구가 예외를 던지면 outcome error가 기록되고 예외가 그대로 전파되는지 확인"""
    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def boom():
        raise ValueError("망함")

    async def run():
        with pytest.raises(ValueError, match="망함"):
            await boom()

    asyncio.run(run())

    h._event_store.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    outcome = lines[1]
    assert outcome["source"] == "outcome"
    assert outcome["outcome"]["status"] == "error"
    assert outcome["outcome"]["severity"] == "warning"


def test_async_tool_denied_releases_lock(tmp_path):
    """deny된 async 호출 후에도 락이 정상 해제돼 다음 호출이 막히지 않는지 확인"""
    from rein.guardrails.exceptions import Denied
    from rein.guardrails.verdict import Verdict

    h = Harness(record=tmp_path / "run.jsonl")

    def deny_stage(tool_call, ctx):
        return Verdict.DENY, "rule_block", "차단", "evt_x"

    h.register_stage("budget", deny_stage)

    @h.register_tool
    async def blocked_tool():
        return "실행되면 안 됨"

    async def run():
        with pytest.raises(Denied):
            await blocked_tool()
        # 락이 정상 해제됐다면 두 번째 호출도 (ConcurrentToolCallError 없이)
        # 똑같이 Denied를 던져야 한다.
        with pytest.raises(Denied):
            await blocked_tool()

    asyncio.run(run())
    assert h._in_flight_tool is None


def test_async_concurrent_gather_rejected(tmp_path):
    """asyncio.gather로 실제 동시 호출하면 하나는 성공, 하나는 ConcurrentToolCallError"""
    from rein.harness import ConcurrentToolCallError

    h = Harness(record=tmp_path / "run.jsonl")

    @h.register_tool
    async def slow_tool():
        await asyncio.sleep(0.05)
        return "done"

    async def run():
        return await asyncio.gather(slow_tool(), slow_tool(), return_exceptions=True)

    results = asyncio.run(run())
    successes = [r for r in results if r == "done"]
    errors = [r for r in results if isinstance(r, ConcurrentToolCallError)]
    assert len(successes) == 1
    assert len(errors) == 1


def test_async_sequential_seq_matches_call_order(tmp_path):
    """두 async 도구를 순서대로 await하면 seq가 호출 순서(0,1)와 일치하는지 확인"""
    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def echo(x: int) -> int:
        return x

    async def run():
        await echo(1)
        await echo(2)

    asyncio.run(run())
    h._event_store.close()

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_wraps = [line for line in lines if line["source"] == "tool_wrap"]
    assert [tw["seq"] for tw in tool_wraps] == [0, 1]
    assert [tw["args"]["x"] for tw in tool_wraps] == [1, 2]


def test_mixed_sync_async_tools_ordering(tmp_path):
    """한 Harness에 sync/async 도구를 섞어 등록해도 순차 호출 시 seq 순서가 정상인지 확인"""
    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    def sync_tool(x: int) -> int:
        return x

    @h.register_tool
    async def async_tool(x: int) -> int:
        return x

    async def run():
        sync_tool(1)
        await async_tool(2)
        sync_tool(3)

    asyncio.run(run())
    h._event_store.close()

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_wraps = [line for line in lines if line["source"] == "tool_wrap"]
    assert [tw["seq"] for tw in tool_wraps] == [0, 1, 2]
    assert [tw["args"]["x"] for tw in tool_wraps] == [1, 2, 3]


def test_async_replay_verify_roundtrip(tmp_path):
    """async Harness로 기록한 run.jsonl을 기존 ReplayEngine(무변경)으로 그대로 재생"""
    from rein.replay.engine import ReplayEngine

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def fetch(url: str) -> str:
        return f"fetched:{url}"

    async def run():
        await fetch("https://a.example")
        await fetch("https://b.example")

    asyncio.run(run())
    h._event_store.close()

    engine = ReplayEngine(log_path, mode="replay-verify")
    assert len(engine) == 2
    first = engine.match("fetch", {"url": "https://a.example"})
    assert first["seq"] == 0
    second = engine.match("fetch", {"url": "https://b.example"})
    assert second["seq"] == 1


def test_async_nested_registered_tool_call_rejected(tmp_path):
    """등록된 async 도구 안에서 또 다른 등록된 async 도구를 await하는
    중첩(재진입) 호출도 asyncio.gather와 동일하게 ConcurrentToolCallError로
    거부되는지 확인 (최종 리뷰 Important #2 — 의도된 계약을 테스트로 고정).
    """
    from rein.harness import ConcurrentToolCallError

    h = Harness(record=tmp_path / "run.jsonl")

    @h.register_tool
    async def inner() -> str:
        return "inner"

    @h.register_tool
    async def outer() -> str:
        return await inner()

    async def run():
        with pytest.raises(ConcurrentToolCallError):
            await outer()

    asyncio.run(run())


def test_async_live_rerun_replays_and_calls_real_tool(tmp_path):
    """async 도구를 record 모드로 먼저 기록한 뒤 live-rerun으로 재실행하면
    실제 함수가 다시 호출되고 위치 매칭이 성립하는지 확인
    (tests/test_harness_live_rerun.py의 sync live-rerun 계약과 동일, M6).
    """
    log_path = tmp_path / "run.jsonl"
    h_record = Harness(record=log_path)

    @h_record.register_tool
    async def add(a: int, b: int) -> int:
        return a + b

    assert asyncio.run(add(2, 3)) == 5
    h_record._event_store.close()

    h_replay = Harness(record=tmp_path / "out.jsonl", mode="live-rerun", replay_from=log_path)

    call_count = 0

    @h_replay.register_tool
    async def add(a: int, b: int) -> int:  # noqa: F811 - 위치 매칭을 위해 같은 tool_name 재사용
        nonlocal call_count
        call_count += 1
        return a + b

    result = asyncio.run(add(2, 3))
    assert result == 5
    assert call_count == 1  # 실제 함수가 다시 호출됐는지 확인
    assert h_replay._replay_engine._cursor == 1


def test_async_tool_reusable_across_separate_event_loops(tmp_path):
    """같은 Harness 인스턴스를 asyncio.run()으로 두 번(별도 이벤트 루프) 호출해도
    정상 동작하고 seq가 이어지는지 확인. asyncio.Lock의 무경합(fast path) 획득은
    이벤트 루프에 바인딩되지 않아 성립하는 성질을 회귀로 고정한다(M9).
    """
    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def echo(x: int) -> int:
        return x

    assert asyncio.run(echo(1)) == 1
    assert asyncio.run(echo(2)) == 2  # 별도의 새 이벤트 루프

    h._event_store.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_wraps = [line for line in lines if line["source"] == "tool_wrap"]
    assert [tw["seq"] for tw in tool_wraps] == [0, 1]

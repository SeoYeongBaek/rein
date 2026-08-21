"""M4 #78: register_tool 비동기(async def) 도구 지원 테스트.

신규 의존성(pytest-asyncio) 추가 없이 asyncio.run()으로 감싼 일반
def test_* 함수로 작성한다(설계 스펙
docs/superpowers/specs/2026-08-21-async-tool-support-design.md 참고).
"""

import asyncio
import json

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

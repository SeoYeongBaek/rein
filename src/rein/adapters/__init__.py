"""모델 어댑터 (CLAUDE.md §3). OpenAI/Claude/로컬 등 프로바이더 비종속화."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

_BUILTIN_CLIENT_MODULE_PREFIXES = ("openai", "anthropic")


@runtime_checkable
class ToolCallExtractor(Protocol):
    """§3 최소 프로토콜. 이 프로토콜은 공개 확장 포인트가 아니라 내장
    어댑터의 내부 구현 디테일이다 — 서드파티 등록 경로는 지금 열지
    않는다(§3, M4에서 별도 설계)."""

    def extract_tool_calls(self, response: Any) -> list[Any]: ...


def is_builtin_model_client(client: Any) -> bool:
    module = type(client).__module__ or ""
    return module.split(".")[0] in _BUILTIN_CLIENT_MODULE_PREFIXES


def is_recognized_adapter(client: Any) -> bool:
    """§3 어댑터 인식 조건: (a) 내장 타입 자동 감지 OR (b) 최소 프로토콜."""
    return is_builtin_model_client(client) or isinstance(client, ToolCallExtractor)

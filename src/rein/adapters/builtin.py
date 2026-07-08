"""내장 모델 클라이언트 타입 자동 감지 (CLAUDE.md §3).

기존 __init__.py에서 분리. 검사 로직은 기존 동작과 1:1 — 모듈
이름의 첫 토큰(prefix)이 내장 SDK 화이트리스트에 포함되는지로 판정.
"""

from __future__ import annotations

from typing import Any

# adapters/__init__.py 에서 임포트해 갈 수 있도록 상수를 외부로 노출합니다.
BUILTIN_OPENAI_PREFIX = "openai"
BUILTIN_ANTHROPIC_PREFIX = "anthropic"

_BUILTIN_CLIENT_MODULE_PREFIXES = (BUILTIN_OPENAI_PREFIX, BUILTIN_ANTHROPIC_PREFIX)


def is_builtin_model_client(client: Any) -> bool:
    """§3 첫 갈래: 내장 타입 자동 감지 (모듈 prefix).

    §3 TODO(현준 확정): "로컬 클라이언트" 자동 감지는 미정. 현재는
    openai/anthropic만 자동 인식되며, 로컬은 §3 두 번째 갈래
    (extract_tool_calls 구현)로만 인식된다.
    """
    if client is None:
        return False
    module = type(client).__module__ or ""
    return module.split(".")[0] in _BUILTIN_CLIENT_MODULE_PREFIXES

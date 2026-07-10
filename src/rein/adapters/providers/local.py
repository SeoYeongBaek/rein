"""로컬 모델 클라이언트 어댑터 (CLAUDE.md §3, 내장, 스켈레톤).

§3 TODO(현준 확정): "로컬 클라이언트"를 타입으로 자동 감지하는
구체적 기준이 아직 미정이다. 현재 구현은 openai/anthropic 모듈 prefix만
자동 인식하며, 로컬 클라이언트는 §3 두 번째 갈래(extract_tool_calls
구현 여부)로만 인식된다.

따라서 이 어댑터는 사용자가 명시적으로 instantiate해서 observe_model에
넘기는 용도다 — 자동 감지 경로로는 들어오지 않는다. 본 PR은 스켈레톤:
로컬 응답 포맷 가정 없이, "어떤 로컬 어댑터든 extract_tool_calls
하나만 구현하면 §3 두 번째 갈래를 탄다"는 인터페이스를 보증한다.

로컬 응답 포맷(vLLM, llama.cpp, Ollama 등)은 M4 "추가 어댑터"
스코프에서 별도 설계한다 — 지금 슬며시 정의하면 M1 스코프 크리프.
"""

from __future__ import annotations

from typing import Any

from rein.adapters.protocol import ToolUse


class LocalAdapter:
    """§3 로컬 어댑터 스켈레톤.

    사용자는 자기 로컬 런타임 응답 포맷에 맞춰 extract_tool_calls
    오버라이드하거나 별도 어댑터를 만들어 observe_model에 넘긴다.
    본 기본 구현은 "tool_use가 없다"는 보수적 가정으로 빈 리스트를
    반환한다 — 로컬 프로바이더별 응답 스키마는 M4.
    """

    def extract_tool_calls(self, response: Any) -> list[ToolUse]:
        """로컬 응답에서 tool_use 추출.

        §3 TODO로 보류: 로컬 응답 포맷 표준화는 M4. 본 스켈레톤은
        보수적으로 빈 리스트를 반환한다 — 자동감지에 안 걸리는
        로컬 클라이언트는 §3 두 번째 갈래(메서드 구현 여부)로만
        인정되며, 그 어댑터 본체는 사용자/외부 어댑터 책임이다.
        """
        return []

"""pytest 전역 fixture."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_adapter_registry(monkeypatch):
    """rein.adapters._ADAPTER_REGISTRY는 프로세스 전역 상태다 (#80).

    register_adapter()로 등록한 서드파티 prefix가 테스트 간에 새지
    않도록, 매 테스트마다 딕셔너리 복사본으로 스왑해 자동 격리한다.
    """
    import rein.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "_ADAPTER_REGISTRY", dict(adapters_mod._ADAPTER_REGISTRY))

"""M1 이전 스모크 테스트. 실제 유닛 테스트는 각 모듈 구현과 함께 추가."""

import rein


def test_package_importable():
    assert rein.__version__


def test_harness_is_context_manager():
    assert hasattr(rein.Harness, "__enter__")
    assert hasattr(rein.Harness, "__exit__")

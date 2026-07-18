"""rules.yaml 로딩과 런타임 최종 판정을 담당함."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from rein import rules

# 기존 CLI의 충돌 해결 순서와 동일하게 유지함.
_VERDICT_PRIORITY = {
    "allow": 0,
    "retry": 1,
    "approve": 2,
    "deny": 3,
}


def load_rules(
    rules_paths: Iterable[str | Path],
) -> list[dict[str, Any]]:
    """YAML 파일의 규칙 문서를 하나의 목록으로 읽음."""
    loaded_rules: list[dict[str, Any]] = []

    for raw_path in rules_paths:
        path = Path(raw_path)

        try:
            text = path.read_text(encoding="utf-8")
            documents = list(yaml.safe_load_all(text))
        except OSError as exc:
            raise ValueError(f"{path} 파일을 읽을 수 없습니다: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} YAML 파싱 실패: {exc}") from exc

        for document in documents:
            # `---` 뒤에 아무 내용도 없는 빈 문서는 무시함.
            if document is None:
                continue

            if not isinstance(document, dict) or "rule" not in document:
                top_level = (
                    sorted(document) if isinstance(document, dict) else type(document).__name__
                )

                warnings.warn(
                    f"{path}: 'rule' 키가 없는 YAML 문서를 건너뜁니다 (최상위: {top_level})",
                    stacklevel=2,
                )
                continue

            rule = document["rule"]

            if not isinstance(rule, dict):
                raise ValueError(f"{path}: rule 값은 매핑이어야 합니다: {rule!r}")

            loaded_rules.append(rule)

    return loaded_rules


def normalize_verdict(value: Any) -> str:
    """verdict를 소문자 문자열로 검증·정규화함."""
    if not isinstance(value, str):
        raise ValueError(f"verdict는 문자열이어야 합니다: {value!r}")

    normalized = value.lower()

    if normalized not in _VERDICT_PRIORITY:
        raise ValueError(f"허용되지 않은 verdict: {value!r} (허용값: {sorted(_VERDICT_PRIORITY)})")

    return normalized


def matching_rules(
    event: dict[str, Any],
    loaded_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """현재 이벤트에 매칭되는 규칙만 반환함."""
    return [rule for rule in loaded_rules if rules.rule_matches(rule, event)]


def verdict_from_rules(
    event: dict[str, Any],
    loaded_rules: list[dict[str, Any]],
) -> str:
    """매칭 규칙 중 가장 제한적인 최종 verdict를 반환함."""
    matched = matching_rules(
        event,
        loaded_rules,
    )

    if not matched:
        return "allow"

    verdicts = [normalize_verdict(rule.get("then", "allow")) for rule in matched]

    return max(
        verdicts,
        key=_VERDICT_PRIORITY.__getitem__,
    )

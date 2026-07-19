"""rules.yaml 로딩과 런타임 최종 판정을 담당함."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from rein import rules
from rein.guardrails.verdict import Verdict


def _load_rules(
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


def matching_rules(
    event: dict[str, Any],
    loaded_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """현재 이벤트에 매칭되는 규칙만 반환함."""
    return [rule for rule in loaded_rules if rules.rule_matches(rule, event)]


def _verdict_from_rules(
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

    try:
        verdicts = [_to_verdict(rule.get("then", "allow")) for rule in matched]
    except ValueError as exc:
        raise ValueError(f"규칙의 then 값이 잘못되었습니다: {matched!r}") from exc

    return str(max(verdicts, key=lambda v: v.value))


def _to_verdict(value: str) -> Verdict:
    """문자열 verdict를 Verdict로 변환함."""
    if not isinstance(value, str):
        raise ValueError(f"허용되지 않은 verdict 타입: {type(value).__name__}={value!r}")

    try:
        return Verdict(value)
    except ValueError:
        try:
            return Verdict[value.upper()]
        except (KeyError, AttributeError) as exc:
            raise ValueError(f"허용되지 않은 verdict: {value!r}") from exc

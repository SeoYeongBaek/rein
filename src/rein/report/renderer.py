"""ReportData를 Jinja2 정적 HTML로 렌더링함."""

from __future__ import annotations

from pathlib import Path

from jinja2 import (
    Environment,
    PackageLoader,
    StrictUndefined,
    TemplateError,
    select_autoescape,
)

from rein.report.builder import ReportError
from rein.report.models import ReportData


def render_report(
    data: ReportData,
    output_path: Path,
) -> None:
    """템플릿을 렌더링해 UTF-8 HTML 파일로 저장함."""
    try:
        environment = Environment(
            loader=PackageLoader(
                "rein.report",
                "templates",
            ),
            autoescape=select_autoescape(["html", "xml"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        template = environment.get_template("report.html.j2")

        html = template.render(report=data)

    except TemplateError as exc:
        raise ReportError(f"HTML 템플릿 렌더링 실패: {exc}") from exc

    try:
        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            html,
            encoding="utf-8",
        )

    except OSError as exc:
        raise ReportError(f"{output_path} 파일을 쓸 수 없습니다: {exc}") from exc

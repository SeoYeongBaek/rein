"""정적 report.html 생성 기능의 공개 진입점."""

from rein.report.builder import (
    ReportError,
    build_report_data,
)
from rein.report.renderer import render_report

__all__ = [
    "ReportError",
    "build_report_data",
    "render_report",
]

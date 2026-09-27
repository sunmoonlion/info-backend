"""数据集的领域对象（0008-info 段二）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DatasetBuildError(RuntimeError):
    """原文不足以建数据集。code 是稳定错误码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class StatementRow:
    statement: str
    security_code: str
    report_date: str  # YYYY-MM-DD
    report_type: str
    fiscal_year: int
    aggregator_notice_date: str | None
    currency: str | None
    values: dict[str, float | None]
    basis: str = "未核实"
    verified: bool = False
    verified_against: str | None = None


@dataclass(frozen=True)
class OfficialFigure:
    """年度报告「主要会计数据」里的一个数。"""

    fiscal_year: int
    item: str
    value: float
    basis: str  # 原始披露 / 追溯调整后 / 比较数
    column_label: str  # 年报里这一列的原样表头，例如「2021年 调整后」
    source_report: str
    report_fiscal_year: int
    disclosed_date: str
    page: int
    revised_report: bool


@dataclass(frozen=True)
class DisclosureEntry:
    fiscal_year: int
    report_type: str
    title: str
    official_disclosed_date: str
    announcement_id: str
    revised: bool
    source: str
    artifact_sha256: str


@dataclass(frozen=True)
class ReportPage:
    page_number: int  # 从 1 起
    tables: tuple[tuple[tuple[str, ...], ...], ...]
    text: str


@dataclass(frozen=True)
class ParsedReport:
    figures: tuple[OfficialFigure, ...]
    explanation: str | None  # 年报对追溯调整的文字说明，原样摘录
    page: int | None
    problem: str | None = None  # 解析不出来时的原因；不猜


@dataclass(frozen=True)
class QualityCheck:
    check_id: str
    name: str
    blocking: bool
    passed: bool
    checked: int
    violations: tuple[dict[str, Any], ...] = ()
    note: str = ""


@dataclass(frozen=True)
class QualityReport:
    checks: tuple[QualityCheck, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.blocking)

    @property
    def failed_blocking(self) -> tuple[str, ...]:
        return tuple(c.check_id for c in self.checks if c.blocking and not c.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_blocking": list(self.failed_blocking),
            "checks": [
                {
                    "check_id": c.check_id,
                    "name": c.name,
                    "blocking": c.blocking,
                    "passed": c.passed,
                    "checked": c.checked,
                    "violations": list(c.violations),
                    "note": c.note,
                }
                for c in self.checks
            ],
        }


@dataclass(frozen=True)
class BuiltDataset:
    dataset_id: str
    data_version: str
    security_code: str
    status: str  # published / quality_failed
    content: bytes  # SQLite 文件
    sha256: str
    row_counts: dict[str, int]
    start_date: str
    end_date: str
    quality: QualityReport
    metadata: dict[str, str] = field(default_factory=dict)

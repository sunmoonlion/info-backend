"""把第三方报表的原始响应解析成报表行。只取财务目录里登记的字段。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable

from app.domain.securities import SecurityCode
from app.domain.securities.dataset import DatasetBuildError, StatementRow
from app.domain.securities.financial_catalog import REPORT_TYPES, STATEMENT_FIELDS

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_statement(
    statement: str, payloads: Iterable[bytes], code: SecurityCode
) -> list[StatementRow]:
    if statement not in STATEMENT_FIELDS:
        raise DatasetBuildError("unknown_statement", statement)
    fields = STATEMENT_FIELDS[statement]
    rows: dict[str, StatementRow] = {}
    for payload in payloads:
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DatasetBuildError("statement_unreadable", statement) from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise DatasetBuildError("statement_unreadable", statement)
        for raw in data:
            row = _row(statement, raw, code, fields)
            previous = rows.get(row.report_date)
            if previous is not None and previous.values != row.values:
                raise DatasetBuildError(
                    "statement_conflict", f"{statement} {row.report_date}"
                )
            rows[row.report_date] = row
    if not rows:
        raise DatasetBuildError("statement_empty", statement)
    return [rows[key] for key in sorted(rows)]


def _row(statement, raw, code: SecurityCode, fields) -> StatementRow:
    if not isinstance(raw, dict):
        raise DatasetBuildError("statement_unreadable", statement)
    if str(raw.get("SECURITY_CODE") or "") != code.code:
        raise DatasetBuildError("statement_of_another_security", statement)
    matched = _DATE.match(str(raw.get("REPORT_DATE") or ""))
    report_type = str(raw.get("REPORT_TYPE") or "")
    if matched is None or report_type not in REPORT_TYPES:
        raise DatasetBuildError("statement_row_invalid", statement)
    notice = _DATE.match(str(raw.get("NOTICE_DATE") or ""))
    values: dict[str, float | None] = {}
    for name, source, _, _ in fields:
        values[name] = _number(raw.get(source), f"{statement}.{name}")
    return StatementRow(
        statement=statement,
        security_code=code.code,
        report_date=matched.group(0),
        report_type=report_type,
        fiscal_year=int(matched.group(1)),
        aggregator_notice_date=notice.group(0) if notice else None,
        currency=(str(raw["CURRENCY"]) if raw.get("CURRENCY") else None),
        values=values,
    )


def _number(value: object, what: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise DatasetBuildError("statement_value_invalid", what)
    try:
        number = float(value)
    except ValueError:
        raise DatasetBuildError("statement_value_invalid", what) from None
    if not math.isfinite(number):
        raise DatasetBuildError("statement_value_invalid", what)
    return number

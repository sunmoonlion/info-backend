"""把解析与检查的结果排成数据集的各张表。写成文件由基础设施层做（端口 `DatasetFileWriter`）。

表的形状与知识服务现有的数据集一致（知识服务的查询类能直接读）：
dataset_metadata 里有 data_snapshot_id、start_date、end_date；口径表叫 metric_dictionary。
口径的可执行定义、表间关系、表的主键是给知识服务的语义层用的（k8s 库 0009-semantic）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from app.domain.securities import SecurityCode
from app.domain.securities.dataset import (
    DisclosureEntry,
    OfficialFigure,
    StatementRow,
)
from app.domain.securities.financial_catalog import (
    METRICS,
    RECONCILIATION_RULES,
    ROW_FIELDS,
    STATEMENT_FIELDS,
    TABLE_KEYS,
    TABLE_LINKS,
)

# 2.0.0：数据集自述第二版。口径表在原有七列之后增加可执行的定义；
# 新增 table_links（表间关系）与 table_keys（一行由什么确定）。原有的列与表不变。
EXPORT_VERSION = "2.0.0"


def dataset_id(code: SecurityCode) -> str:
    return f"{code.prefixed.lower()}-financials"


def _statement_table(statement: str, rows: Sequence[StatementRow]) -> tuple[list, list]:
    names = [name for name, *_ in STATEMENT_FIELDS[statement]]
    columns = [
        ("security_code", "TEXT"),
        ("report_date", "TEXT"),
        ("report_type", "TEXT"),
        ("fiscal_year", "INTEGER"),
        ("basis", "TEXT"),
        ("verified", "INTEGER"),
        ("verified_against", "TEXT"),
        ("aggregator_notice_date", "TEXT"),
        ("currency", "TEXT"),
        *[(n, "REAL") for n in names],
    ]
    data = [
        (
            r.security_code,
            r.report_date,
            r.report_type,
            r.fiscal_year,
            r.basis,
            1 if r.verified else 0,
            r.verified_against,
            r.aggregator_notice_date,
            r.currency,
            *[r.values.get(n) for n in names],
        )
        for r in sorted(rows, key=lambda r: r.report_date)
    ]
    return columns, data


def build_tables(
    rows_by_statement: dict[str, list[StatementRow]],
    figures: Sequence[OfficialFigure],
    calendar: Sequence[DisclosureEntry],
) -> dict[str, tuple[list, list]]:
    tables: dict[str, tuple[list, list]] = {}
    for statement in STATEMENT_FIELDS:
        tables[statement] = _statement_table(statement, rows_by_statement[statement])
    tables["official_key_figures"] = (
        [
            ("fiscal_year", "INTEGER"),
            ("item", "TEXT"),
            ("value", "REAL"),
            ("basis", "TEXT"),
            ("column_label", "TEXT"),
            ("source_report", "TEXT"),
            ("disclosed_date", "TEXT"),
            ("page", "INTEGER"),
            ("revised_report", "INTEGER"),
        ],
        sorted(
            {
                (
                    f.fiscal_year,
                    f.item,
                    f.value,
                    f.basis,
                    f.column_label,
                    f.source_report,
                    f.disclosed_date,
                    f.page,
                    1 if f.revised_report else 0,
                )
                for f in figures
            }
        ),
    )
    tables["disclosure_calendar"] = (
        [
            ("fiscal_year", "INTEGER"),
            ("report_type", "TEXT"),
            ("title", "TEXT"),
            ("official_disclosed_date", "TEXT"),
            ("announcement_id", "TEXT"),
            ("revised", "INTEGER"),
            ("source", "TEXT"),
            ("artifact_sha256", "TEXT"),
        ],
        sorted(
            (
                e.fiscal_year,
                e.report_type,
                e.title,
                e.official_disclosed_date,
                e.announcement_id,
                1 if e.revised else 0,
                e.source,
                e.artifact_sha256,
            )
            for e in calendar
        ),
    )
    dictionary = [
        (statement, name, display, unit)
        for statement, fields in STATEMENT_FIELDS.items()
        for name, _, display, unit in fields
    ] + [
        (statement, name, display, "")
        for statement in STATEMENT_FIELDS
        for name, display in ROW_FIELDS
    ]
    tables["field_dictionary"] = (
        [
            ("source_table", "TEXT"),
            ("field", "TEXT"),
            ("display_name", "TEXT"),
            ("unit", "TEXT"),
        ],
        dictionary,
    )
    tables["metric_dictionary"] = (
        [
            ("metric_name", "TEXT"),
            ("display_name", "TEXT"),
            ("source_table", "TEXT"),
            ("expression_hint", "TEXT"),
            ("unit", "TEXT"),
            ("time_basis", "TEXT"),
            ("description", "TEXT"),
            ("kind", "TEXT"),
            ("base_table", "TEXT"),
            ("value_expression", "TEXT"),
            ("applicable_when", "TEXT"),
            ("reason_if_not", "TEXT"),
            ("queryable", "INTEGER"),
        ],
        [
            (
                m.metric_name,
                m.display_name,
                m.source_table,
                m.expression_hint,
                m.unit,
                m.time_basis,
                m.description,
                "row",
                m.base_table,
                m.value_expression,
                m.applicable_when,
                m.reason_if_not,
                1 if m.queryable else 0,
            )
            for m in METRICS
        ],
    )
    tables["table_links"] = (
        [
            ("link_name", "TEXT"),
            ("from_table", "TEXT"),
            ("to_table", "TEXT"),
            ("cardinality", "TEXT"),
            ("on_columns", "TEXT"),
        ],
        [
            (
                link.link_name,
                link.from_table,
                link.to_table,
                link.cardinality,
                ",".join(link.on_columns),
            )
            for link in TABLE_LINKS
        ],
    )
    tables["table_keys"] = (
        [
            ("table_name", "TEXT"),
            ("key_columns", "TEXT"),
            ("label_columns", "TEXT"),
        ],
        [
            (key.table_name, ",".join(key.key_columns), ",".join(key.label_columns))
            for key in TABLE_KEYS
        ],
    )
    tables["reconciliation_rules"] = (
        [
            ("rule_id", "TEXT"),
            ("rule", "TEXT"),
            ("source_table", "TEXT"),
            ("residual_expression", "TEXT"),
        ],
        [
            (r.rule_id, r.rule, r.source_table, r.residual_expression)
            for r in RECONCILIATION_RULES
        ],
    )
    return tables


def content_fingerprint(tables: dict[str, tuple[list, list]]) -> str:
    """数据版本由内容决定：同样的原文、同样的目录，得到同样的版本。"""
    canonical = json.dumps(
        {
            name: {"columns": cols, "rows": rows}
            for name, (cols, rows) in tables.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=list,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

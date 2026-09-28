"""东方财富 F10 财务分析：三大报表，按报告期。第三方网站，仅内部使用（F-INFO-03）。

接口地址与参数参考了 akshare 1.18.97（MIT License，Copyright (c) 2019-2026 Albert King）
的 stock_balance_sheet_by_report_em、stock_profit_sheet_by_report_em、
stock_cash_flow_sheet_by_report_em；运行时不依赖 akshare（F-INFO-09）。
"""

from __future__ import annotations

import re

from app.application.securities.collectors.base import (
    CollectContext,
    parse_json,
    require,
)
from app.domain.securities import (
    CollectError,
    FetchRequest,
    ItemKind,
    SecurityCode,
    SourceCode,
)

_BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
# 报表名 → 接口前缀
STATEMENTS: dict[str, str] = {
    "balance_sheet": "zcfzb",
    "income_statement": "lrb",
    "cash_flow": "xjllb",
}
_GENERAL_COMPANY = "4"  # 一般工商业；银行、保险、券商的报表结构不同，不在第一切口
_COMPANY_TYPE = re.compile(
    r'<input[^>]*\bid="hidctype"[^>]*\bvalue="(\d{1,2})"'
    r'|<input[^>]*\bvalue="(\d{1,2})"[^>]*\bid="hidctype"'
)
_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_BATCH = 5
_MAX_PERIODS = 400


class EastmoneyF10StatementCollector:
    source = SourceCode.EASTMONEY_F10.value

    async def collect(self, code: SecurityCode, ctx: CollectContext) -> None:
        # 沪、深、京三个市场的接口相同，只是代码前缀不同（北交所 2026-09-28 实测）
        company_type = await self._company_type(code, ctx)
        for statement, prefix in STATEMENTS.items():
            periods = await self._periods(code, company_type, statement, prefix, ctx)
            for index in range(0, len(periods), _BATCH):
                batch = periods[index : index + _BATCH]
                content = await ctx.fetch(
                    FetchRequest(
                        source=SourceCode.EASTMONEY_F10,
                        kind=ItemKind.STATEMENT_DATA,
                        url=f"{_BASE}/{prefix}AjaxNew",
                        params=(
                            ("companyType", company_type),
                            ("reportDateType", "0"),
                            ("reportType", "1"),
                            ("dates", ",".join(batch)),
                            ("code", code.prefixed),
                        ),
                        name=f"{statement}-{batch[-1]}_{batch[0]}.json",
                        meta={"statement": statement, "periods": batch},
                    )
                )
                payload = parse_json(content, what=f"{statement} data")
                rows = payload.get("data") if isinstance(payload, dict) else None
                require(isinstance(rows, list) and len(rows) > 0, f"{statement} data")

    async def _company_type(self, code: SecurityCode, ctx: CollectContext) -> str:
        content = await ctx.fetch(
            FetchRequest(
                source=SourceCode.EASTMONEY_F10,
                kind=ItemKind.COMPANY_TYPE_PAGE,
                url=f"{_BASE}/Index",
                params=(("type", "web"), ("code", code.prefixed.lower())),
                name="finance-index.html",
            )
        )
        matched = _COMPANY_TYPE.search(content.decode("utf-8", errors="replace"))
        if matched is None:
            raise CollectError("unexpected_response", "company type")
        company_type = matched.group(1) or matched.group(2)
        if company_type != _GENERAL_COMPANY:
            raise CollectError("unsupported_company_type", company_type)
        return company_type

    async def _periods(
        self,
        code: SecurityCode,
        company_type: str,
        statement: str,
        prefix: str,
        ctx: CollectContext,
    ) -> list[str]:
        content = await ctx.fetch(
            FetchRequest(
                source=SourceCode.EASTMONEY_F10,
                kind=ItemKind.STATEMENT_PERIODS,
                url=f"{_BASE}/{prefix}DateAjaxNew",
                params=(
                    ("companyType", company_type),
                    ("reportDateType", "0"),
                    ("code", code.prefixed),
                ),
                name=f"{statement}-periods.json",
                meta={"statement": statement},
            )
        )
        payload = parse_json(content, what=f"{statement} periods")
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise CollectError("unexpected_response", f"{statement} periods")
        periods: list[str] = []
        for row in rows:
            matched = (
                _PERIOD.match(str(row.get("REPORT_DATE") or ""))
                if isinstance(row, dict)
                else None
            )
            if matched is None:
                raise CollectError("unexpected_response", f"{statement} period")
            periods.append(matched.group(1))
        require(len(periods) <= _MAX_PERIODS, f"{statement} period count")
        return list(dict.fromkeys(periods))

"""Wind 动态补采任务（WP-5）：接收财务数据缺口需求，调用 Wind 适配器获取事实，生成标准化交付件。

遵循规范：
- 默认支持 Replay 模式，无凭据时不发起付费外部调用。
- 产出结构化原始事实 JSON，支持按法定披露或用户采集原件格式交付给 knowbase。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.infrastructure.external.wind_adapter import WindAdapter, WindError
from app.worker import celery_app

logger = logging.getLogger(__name__)


async def execute_wind_fetch(
    subject: str,
    fields: list[str],
    report_date: str,
    *,
    doc_type: str = "annual_report",
    beneficiaries: list[dict[str, Any]] | None = None,
    replay_mode: bool | None = None,
    knowbase_client: Any = None,
) -> dict[str, Any]:
    """执行 Wind 数据动态获取与交付处理核心逻辑。"""
    adapter = WindAdapter(replay_mode=replay_mode)
    res = await adapter.fetch_financials(
        subject=subject,
        chart_fields=fields,
        report_date=report_date,
    )

    if not res.get("matched"):
        return {
            "status": "gap",
            "reason": "data_not_available_in_source",
            "subject": subject,
            "report_date": report_date,
            "fields": fields,
            "facts": {},
        }

    facts = res["facts"]
    payload_data = {
        "source": "wind",
        "subject": subject,
        "doc_type": doc_type,
        "report_date": report_date,
        "mode": res.get("mode", "live"),
        "fields": fields,
        "facts": facts,
    }
    content_bytes = json.dumps(payload_data, ensure_ascii=False, indent=2).encode(
        "utf-8"
    )
    channel_doc_id = f"wind:{subject}:{report_date}"
    name = f"{subject}_{report_date}_wind_financials.json"

    material_id = None
    version_no = None

    if knowbase_client is not None:
        if beneficiaries:
            # 用户名下采集原件交付
            delivered = await knowbase_client.deliver_collected(
                channel="wind",
                channel_doc_id=channel_doc_id,
                name=name,
                content=content_bytes,
                license="self",
                source_url="https://www.wind.com.cn/",
                beneficiaries=beneficiaries,
            )
            if delivered:
                material_id = delivered[0].get("material_id")
                version_no = delivered[0].get("version_no")
        else:
            # 公开披露层交付
            delivered_pub = await knowbase_client.deliver_public(
                channel="wind",
                channel_doc_id=channel_doc_id,
                name=name,
                content=content_bytes,
                subject=subject,
                doc_type=doc_type,
                period_start=None,
                period_end=None,
                edition="full",
                topics=("finance", "financial_report"),
                disclosed_on=report_date,
                source_url="https://www.wind.com.cn/",
            )
            material_id = delivered_pub.get("material_id")
            version_no = delivered_pub.get("current_version", 1)

    return {
        "status": "delivered" if material_id else "fetched",
        "subject": subject,
        "doc_type": doc_type,
        "report_date": report_date,
        "channel": "wind",
        "channel_doc_id": channel_doc_id,
        "material_id": material_id,
        "version_no": version_no,
        "facts": facts,
        "payload_bytes_len": len(content_bytes),
    }


@celery_app.task(name="app.tasks.wind_fetch_financials")
def wind_fetch_financials(
    subject: str,
    fields: list[str],
    report_date: str,
    doc_type: str = "annual_report",
    beneficiaries: list[dict[str, Any]] | None = None,
    replay_mode: bool | None = None,
) -> dict[str, Any]:
    """Celery 任务入口。"""
    try:
        return asyncio.run(
            execute_wind_fetch(
                subject=subject,
                fields=fields,
                report_date=report_date,
                doc_type=doc_type,
                beneficiaries=beneficiaries,
                replay_mode=replay_mode,
            )
        )
    except WindError as exc:
        logger.warning("Wind fetch failed with business error: %s", exc)
        return {"status": "failed", "error": type(exc).__name__, "message": str(exc)}
    except Exception as exc:
        logger.error(
            "Unexpected error in wind_fetch_financials: %s", exc, exc_info=True
        )
        return {"status": "error", "error": type(exc).__name__, "message": str(exc)}

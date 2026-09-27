"""按证券代码采集一个批次：年报公告与原文、三大报表的原始响应（0008-info 段一）。

退出码：0 成功；2 批次失败（原因在输出的 error_code）；1 参数或运行环境有问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from app.bootstrap.securities import build_security_ingestion_service
from app.domain.securities import IngestionStatus, InvalidSecurityCode
from app.infrastructure.storage.postgres import get_postgres


async def run(code: str):
    postgres = get_postgres()
    try:
        await postgres.init()
        service = build_security_ingestion_service(postgres.session_factory)
        return await service.ingest(code)
    finally:
        await postgres.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="六位 A 股证券代码，例如 600009")
    args = parser.parse_args()
    try:
        summary = asyncio.run(run(args.code))
    except InvalidSecurityCode as exc:
        print(json.dumps({"error": "invalid_security_code", "message": str(exc)}))
        raise SystemExit(1) from None
    except Exception as exc:
        # 不把地址、参数、连接串带到输出里
        print(json.dumps({"error": "ingestion_crashed", "type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(asdict(summary), ensure_ascii=False, default=str))
    raise SystemExit(0 if summary.status is IngestionStatus.SUCCEEDED else 2)


if __name__ == "__main__":
    main()

"""从采集批次建数据集：口径判定、质量检查、产出 SQLite 文件（0008-info 段二）。

默认用该代码最近一个成功的批次。加 `--register` 时，发布后向知识服务登记（段三）。
退出码：0 已发布（要求登记时也已登记）；2 质量检查没过，数据集不发布；
3 已发布但登记没成功；1 参数、原文或运行环境有问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from app.bootstrap.securities import (
    build_dataset_registration_service,
    build_security_dataset_service,
)
from app.domain.securities import InvalidSecurityCode
from app.domain.securities.dataset import DatasetBuildError
from app.domain.securities.registration import RegistrationError
from app.infrastructure.models.securities import SecurityDataset
from app.infrastructure.storage.object_storage import get_object_storage
from app.infrastructure.storage.postgres import get_postgres


async def run(args):
    postgres = get_postgres()
    try:
        await postgres.init()
        service = build_security_dataset_service(postgres.session_factory)
        summary = await (
            service.build(args.ingestion)
            if args.ingestion
            else service.build_latest(args.code)
        )
        if args.out:
            async with postgres.session_factory() as session:
                record = await session.get(SecurityDataset, summary.record_id)
                if record is None:
                    raise DatasetBuildError("dataset_record_missing")
                key, version, sha256 = (
                    record.object_key,
                    record.version_id,
                    record.sha256,
                )
            data = await asyncio.to_thread(
                get_object_storage().get_bytes,
                object_key=key,
                version_id=version,
                expected_sha256=sha256,
            )
            await asyncio.to_thread(Path(args.out).write_bytes, data)
        registration = None
        if args.register and summary.status == "published":
            registrar = build_dataset_registration_service(postgres.session_factory)
            try:
                done = await registrar.register(summary.record_id)
                registration = {"registered": done.registered}
            except RegistrationError as exc:
                registration = {
                    "registered": False,
                    "error": exc.code,
                    "detail": exc.detail,
                    "retryable": exc.retryable,
                }
        return summary, registration
    finally:
        await postgres.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--code", help="六位 A 股证券代码；用它最近一个成功的批次")
    target.add_argument("--ingestion", help="指定采集批次的标识")
    parser.add_argument("--out", help="把数据集文件另存到这个路径")
    parser.add_argument(
        "--register", action="store_true", help="发布后向知识服务登记这个版本"
    )
    args = parser.parse_args()
    try:
        summary, registration = asyncio.run(run(args))
    except InvalidSecurityCode as exc:
        print(json.dumps({"error": "invalid_security_code", "message": str(exc)}))
        raise SystemExit(1) from None
    except DatasetBuildError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
        raise SystemExit(1) from None
    except Exception as exc:
        print(
            json.dumps({"error": "dataset_build_crashed", "type": type(exc).__name__})
        )
        raise SystemExit(1) from None
    output = asdict(summary)
    if registration is not None:
        output["registration"] = registration
    print(json.dumps(output, ensure_ascii=False, default=str))
    if summary.status != "published":
        raise SystemExit(2)
    if registration is not None and not registration["registered"]:
        raise SystemExit(3)
    raise SystemExit(0)


if __name__ == "__main__":
    main()

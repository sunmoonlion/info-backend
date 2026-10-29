"""生成 knowfeed 当提供方的契约 `knowfeed-supply-status/v1` 的 JSON Schema。

模型（`status_event.py`）是唯一真源，测试核父仓的文件与模型逐字一致。改了模型就在
后端目录下重新生成：python -m app.domain.supply.schema
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.supply.status_event import (
    CONTRACT,
    RECEIVER_PATH,
    RELATION,
    DemandStatusEventV1,
)

NAME = "supply-status"
MAJOR = 1
BASE = "https://contracts.sunmoonai.com/knowfeed/supply-status/v1/"
DOCUMENTS: dict[str, tuple[type[BaseModel], str]] = {
    "demand-status.schema.json": (
        DemandStatusEventV1,
        "knowfeed-supply-status v1 demand status event",
    ),
}


def document(name: str) -> dict[str, Any]:
    model, title = DOCUMENTS[name]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": BASE + name,
        **model.model_json_schema(),
        "title": title,
    }


def render(name: str) -> str:
    return json.dumps(document(name), ensure_ascii=False, indent=2) + "\n"


def manifest(texts: dict[str, str]) -> str:
    body = {
        "contract": CONTRACT,
        "major_version": MAJOR,
        "receiver_path": RECEIVER_PATH,
        "relation": RELATION,
        "schemas": {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in sorted(texts.items())
        },
    }
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"


def contract_dir() -> Path:
    return Path(__file__).resolve().parents[5] / "contracts" / NAME / f"v{MAJOR}"


def main() -> None:
    target = contract_dir()
    target.mkdir(parents=True, exist_ok=True)
    texts = {name: render(name) for name in DOCUMENTS}
    for name, text in texts.items():
        (target / name).write_text(text, encoding="utf-8")
    (target / "contract-manifest.json").write_text(manifest(texts), encoding="utf-8")


if __name__ == "__main__":
    main()

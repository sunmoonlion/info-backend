"""财报抽表写的科目编码照 knowbase 的科目表：锁与真源一致，编码都在真源里。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.application.supply.processing import FIELD_TO_CODE

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowbase-chart-lock.json"


def _chart() -> dict:
    if not LOCK.exists():  # 只拿到后端子仓时没有父仓
        pytest.skip("parent contracts/ is not checked out")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    path = PARENT.parent / "knowbase-app" / lock["schema"]
    if not path.exists():
        pytest.skip("knowbase-app is not checked out next to knowfeed-app")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == lock["sha256"]
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_mapped_code_is_in_the_knowbase_chart():
    codes = {item["code"] for item in _chart()["items"]}
    assert sorted(set(FIELD_TO_CODE.values()) - codes) == []

"""knowfeed 的词表副本照 knowbase 词表第 2 版（SDD 0049）：锁与真源一致，副本与真源一致。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.domain.supply import vocabulary

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowbase-vocabulary-lock.json"


def _vocabulary() -> dict:
    if not LOCK.exists():  # 只拿到后端子仓时没有父仓
        pytest.skip("parent contracts/ is not checked out")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    path = PARENT.parent / "knowbase-app" / lock["schema"]
    if not path.exists():
        pytest.skip("knowbase-app is not checked out next to knowfeed-app")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == lock["sha256"]
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_copy_is_the_knowbase_vocabulary():
    source = _vocabulary()
    assert source["version"] == vocabulary.VOCAB_VERSION
    assert vocabulary.DOC_TYPE_CLASS == {
        t: d["class"] for t, d in source["doc_types"].items()
    }
    assert set(vocabulary.PERIODIC) == set(source["periodic"])
    assert vocabulary.TOPICS == tuple(source["topics"])
    assert vocabulary.EDITIONS == tuple(source["editions"])
    assert set(vocabulary.types_of("disclosure")) >= {
        "annual_report",
        "interim_announcement",
    }

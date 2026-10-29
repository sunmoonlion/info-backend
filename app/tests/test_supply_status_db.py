"""需求状态推送（SDD 0045 第 2.5 节，契约 knowfeed-supply-status/v1）与原件留存到期删内容
（第 3.4 节）。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_db import KEY, World

from app.application.supply.notify import StatusNotifier
from app.application.supply.retention import OriginalRetention
from app.domain.supply.schema import DOCUMENTS, contract_dir, manifest, render
from app.domain.supply.status_event import RECEIVER_PATH, DemandStatusEventV1
from app.infrastructure.external.app_supply_status import (
    HttpStatusSender,
    StatusNotDelivered,
    status_relation,
)
from app.infrastructure.repositories.supply import SqlSupplyRepository
from app.infrastructure.repositories.supply_catalog import SqlCatalogRepository
from app.infrastructure.repositories.supply_status import STATUS_TOPIC
from app.infrastructure.storage.object_storage import ObjectStorage
from core.config import get_settings

REGENERATE = "regenerate: python -m app.domain.supply.schema"


async def events(db) -> list[dict]:
    from sqlalchemy import text

    async with db() as s:
        rows = await s.execute(
            text(
                "SELECT payload FROM outbox_message WHERE topic = :t ORDER BY created_at"
            ),
            {"t": STATUS_TOPIC},
        )
        return [r[0] for r in rows.all()]


async def test_every_status_change_queues_one_notice_for_the_app(db):
    w = World(db)
    w.confirm("conf-1", "alice", KEY)
    demand, _, _ = await w.submit()
    # 受理时的状态在答复里，不另发
    assert await events(db) == []
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    assert await w.runner.run(job_id) == "done"
    sent = await events(db)
    assert len(sent) == 1
    notice = DemandStatusEventV1.model_validate(sent[0])
    assert (notice.demand_id, notice.app, notice.owner, notice.status) == (
        demand.id,
        "knowbench",
        "alice",
        "ready",
    )
    assert [(i.confirmation_item, i.status) for i in notice.items] == [("c1", "ready")]
    # 已结束的再来一次不改、不发
    await w.demands.cancel("knowbench", demand.id)
    assert len(await events(db)) == 1


class Sender:
    def __init__(self, configured: bool) -> None:
        self._configured = configured
        self.sent: list[tuple[str, dict]] = []

    def configured(self, app):
        return self._configured

    async def send(self, app, body):
        self.sent.append((app, body))


def notice(**overrides) -> dict:
    body = {
        "contract": "knowfeed-supply-status/v1",
        "event_id": "e-1",
        "demand_id": "d-1",
        "app": "knowlab",
        "owner": "alice",
        "task_ref": None,
        "status": "unsatisfiable",
        "items": [
            {
                "item_id": "i-1",
                "confirmation_item": "c1",
                "status": "unsatisfiable",
                "reason": "no_channel",
                "updated_at": "2026-10-29T08:00:00Z",
            }
        ],
        "occurred_at": "2026-10-29T08:00:00Z",
    }
    return {**body, **overrides}


async def test_the_notifier_skips_apps_whose_receiver_is_not_configured():
    off = Sender(configured=False)
    assert await StatusNotifier(off).deliver(notice()) == "skipped"
    assert off.sent == []
    on = Sender(configured=True)
    assert await StatusNotifier(on).deliver(notice()) == "sent"
    assert on.sent[0][0] == "knowlab" and on.sent[0][1]["demand_id"] == "d-1"
    with pytest.raises(ValueError):
        await StatusNotifier(on).deliver(notice(app="knowmart"))


ENV = {
    "KNOWLAB_INTERNAL_URL": "https://knowlab-backend.knowlab.svc.cluster.local",
    "KNOWLAB_SUPPLY_STATUS_SERVICE_CLIENT_ID": "knowfeed-supply-status",
    "KNOWLAB_SUPPLY_STATUS_SERVICE_CLIENT_SECRET": "s3cret",
    "KNOWLAB_SUPPLY_STATUS_SERVICE_SCOPE": "knowlab:supply-status",
    "KNOWLAB_SUPPLY_STATUS_SERVICE_DISCOVERY_URL": "https://casdoor/.well-known/openid-configuration",
    "KNOWLAB_SUPPLY_STATUS_SERVICE_BACKCHANNEL_ENDPOINT": "http://casdoor:8000",
}


class Tokens:
    async def get_token(self):
        return "service-token"


async def test_the_sender_posts_to_the_receiver_with_its_own_identity():
    assert status_relation("knowlab", ENV) is not None
    assert status_relation("knowbench", ENV) is None
    assert (
        status_relation("knowlab", {**ENV, "KNOWLAB_SUPPLY_STATUS_SERVICE_SCOPE": "x"})
        is None
    )
    assert (
        status_relation(
            "knowlab", {**ENV, "KNOWLAB_INTERNAL_URL": "http://knowlab-backend"}
        )
        is None
    )
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202 if len(seen) == 1 else 503)

    sender = HttpStatusSender(environ=ENV, transport=httpx.MockTransport(answer))
    sender._tokens["knowlab"] = Tokens()  # type: ignore[assignment]
    assert sender.configured("knowlab") and not sender.configured("knowbench")
    await sender.send("knowlab", notice())
    assert seen[0].url.path == RECEIVER_PATH
    assert seen[0].headers["authorization"] == "Bearer service-token"
    assert json.loads(seen[0].content)["event_id"] == "e-1"
    with pytest.raises(StatusNotDelivered) as caught:
        await sender.send("knowlab", notice())
    assert "503" in str(caught.value) and "s3cret" not in str(caught.value)


def _require_contract() -> Path:
    target = contract_dir()
    if not target.exists():  # 只拿到后端子仓时没有父仓
        pytest.skip("parent contracts/ is not checked out")
    return target


def test_the_published_schema_is_generated_from_the_model():
    target = _require_contract()
    texts = {}
    for name in DOCUMENTS:
        texts[name] = (target / name).read_text(encoding="utf-8")
        assert texts[name] == render(name), REGENERATE
    assert (target / "contract-manifest.json").read_text(encoding="utf-8") == manifest(
        texts
    ), REGENERATE
    digest = hashlib.sha256(texts["demand-status.schema.json"].encode()).hexdigest()
    assert json.loads((target / "contract-manifest.json").read_text())["schemas"] == {
        "demand-status.schema.json": digest
    }


async def test_originals_lose_their_content_thirty_days_after_delivery(db):
    w = World(db)
    w.confirm("conf-1", "alice", KEY)
    await w.submit()
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    await w.runner.run(job_id)
    [key] = list(w.objects.data)
    retention = OriginalRetention(
        SqlCatalogRepository(lambda: db), w.objects, clock=w.clock
    )
    w.clock.now += timedelta(days=29)
    assert await retention.purge_due() == 0
    w.clock.now += timedelta(days=1, seconds=1)
    assert await retention.purge_due() == 1
    assert w.objects.data == {}
    # 记录留着：出处、摘要、交给 knowbase 的编号都在，记下删的时间
    row = await sql(
        db,
        "SELECT knowbase_material_id FROM supply_original WHERE purged_at IS NOT NULL",
    )
    assert row == "m-1"
    assert await retention.purge_due() == 0

    # 以后又取到同一份：内容放回去，从头再留 30 天
    w.confirm("conf-2", "bob", KEY)
    await w.submit(owner="bob", confirmation="conf-2", idem="idem-bob-0001")
    job2 = str(await sql(db, "SELECT id FROM supply_job WHERE status = 'queued'"))
    assert await w.runner.run(job2) == "done"
    assert list(w.objects.data) == [key]
    assert await sql(db, "SELECT purged_at FROM supply_original") is None
    # 到期时间按这次交付从头记，刚放回的内容不会被下一轮清理删掉
    assert await retention.purge_due() == 0
    assert list(w.objects.data) == [key]


async def test_a_revived_original_is_not_purged_before_it_is_delivered_again(db):
    """内容放回去到再交给 knowbase 之间，到期时间是空的，清理不碰它。"""
    w = World(db)
    w.confirm("conf-1", "alice", KEY)
    await w.submit()
    await w.runner.run(str(await sql(db, "SELECT id FROM supply_job")))
    repo = SqlCatalogRepository(lambda: db)
    retention = OriginalRetention(repo, w.objects, clock=w.clock)
    w.clock.now += timedelta(days=31)
    assert await retention.purge_due() == 1
    original = str(await sql(db, "SELECT id FROM supply_original"))
    await SqlSupplyRepository(lambda: db).revive_original(original)
    assert await sql(db, "SELECT purge_after FROM supply_original") is None
    assert await retention.purge_due() == 0


class FailingObjects:
    async def delete(self, key):
        raise OSError("storage down")


async def test_a_purge_that_fails_is_tried_again_next_round(db):
    w = World(db)
    w.confirm("conf-1", "alice", KEY)
    await w.submit()
    await w.runner.run(str(await sql(db, "SELECT id FROM supply_job")))
    w.clock.now += timedelta(days=31)
    repo = SqlCatalogRepository(lambda: db)
    assert (
        await OriginalRetention(repo, FailingObjects(), clock=w.clock).purge_due() == 0
    )
    assert await sql(db, "SELECT purged_at FROM supply_original") is None
    assert await OriginalRetention(repo, w.objects, clock=w.clock).purge_due() == 1


def test_local_storage_deletes_the_object_and_its_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET", "development-knowfeed-originals")
    get_settings.cache_clear()
    try:
        storage = ObjectStorage()
        storage.put_bytes(
            object_key="supply/originals/ab/abc",
            data=b"%PDF",
            content_type="application/pdf",
            metadata={"a": "b"},
        )
        assert storage.delete_all_versions(object_key="supply/originals/ab/abc") == 2
        assert storage.delete_all_versions(object_key="supply/originals/ab/abc") == 0
        with pytest.raises(RuntimeError):
            storage.delete_all_versions(object_key="../../etc/passwd")
    finally:
        get_settings.cache_clear()


class FakeS3:
    """开了版本的桶：一个键两版加一个删除标记，另有一个前缀相同的别的键。"""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.closed = False

    def list_object_versions(self, Bucket, Prefix, **marker):
        if not marker:
            return {
                "Versions": [
                    {"Key": Prefix, "VersionId": "v1"},
                    {"Key": Prefix + "-other", "VersionId": "o1"},
                ],
                "IsTruncated": True,
                "NextKeyMarker": Prefix,
                "NextVersionIdMarker": "v1",
            }
        return {
            "Versions": [{"Key": Prefix, "VersionId": "v2"}],
            "DeleteMarkers": [{"Key": Prefix, "VersionId": "d1"}],
            "IsTruncated": False,
        }

    def delete_object(self, Bucket, Key, VersionId):
        self.deleted.append((Key, VersionId))

    def close(self):
        self.closed = True


def test_s3_deletes_every_version_of_that_key_only(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    get_settings.cache_clear()
    try:
        storage = ObjectStorage()
        fake = FakeS3()
        monkeypatch.setattr(storage, "s3_client", lambda: fake)
        assert storage.delete_all_versions(object_key="supply/originals/ab/abc") == 3
        assert fake.deleted == [
            ("supply/originals/ab/abc", "v1"),
            ("supply/originals/ab/abc", "v2"),
            ("supply/originals/ab/abc", "d1"),
        ]
        assert fake.closed
    finally:
        get_settings.cache_clear()

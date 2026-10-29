"""需求接口按关系认调用方、调 knowbase 用这条关系自己的身份（10-29 Fable 回执第 1、2 条）。"""

from __future__ import annotations

from app.infrastructure.external.knowbase_supply import (
    SUPPLY_PATH,
    supply_relation,
    supply_settings,
)
from app.infrastructure.security.relation_auth import relation_bindings
from core.config import Settings

CALLERS = {
    "SERVICE_AUTH_SUPPLY_DEMAND_CALLERS": "knowbench,knowlab,knowmart",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWBENCH_AUDIENCE": "bench-client",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWBENCH_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-knowbench-knowfeed-supply-demand"
    ),
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWBENCH_REQUIRED_SCOPE": "knowfeed:supply-demand",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWLAB_AUDIENCE": "lab-client",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWLAB_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-knowlab-knowfeed-supply-demand"
    ),
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWLAB_REQUIRED_SCOPE": "knowfeed:supply-demand",
    # 部署多配了别的调用方也不认
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWMART_AUDIENCE": "mart-client",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWMART_SUBJECT_ALLOWLIST": "admin/x",
    "SERVICE_AUTH_SUPPLY_DEMAND_KNOWMART_REQUIRED_SCOPE": "knowfeed:supply-demand",
}


def test_each_caller_has_its_own_audience_and_only_bench_and_lab_count() -> None:
    bindings = relation_bindings("supply-demand", CALLERS)
    assert [(b.caller, b.audience) for b in bindings] == [
        ("knowbench", "bench-client"),
        ("knowlab", "lab-client"),
    ]
    wrong_scope = {
        **CALLERS,
        "SERVICE_AUTH_SUPPLY_DEMAND_KNOWLAB_REQUIRED_SCOPE": "knowbase:read",
    }
    assert [b.caller for b in relation_bindings("supply-demand", wrong_scope)] == [
        "knowbench"
    ]
    assert relation_bindings("supply-demand", {}) == []


SUPPLY = {
    "KNOWBASE_INTERNAL_URL": "https://knowbase-tls.ns.svc.cluster.local",
    "KNOWBASE_USER_DATA_SUPPLY_SERVICE_CLIENT_ID": "feed-supply",
    "KNOWBASE_USER_DATA_SUPPLY_SERVICE_CLIENT_SECRET": "s",
    "KNOWBASE_USER_DATA_SUPPLY_SERVICE_SCOPE": "knowbase:user-data-supply",
    "KNOWBASE_USER_DATA_SUPPLY_SERVICE_DISCOVERY_URL": (
        "https://casdoor/.well-known/openid-configuration"
    ),
    "KNOWBASE_USER_DATA_SUPPLY_SERVICE_BACKCHANNEL_ENDPOINT": "https://casdoor",
}


def test_the_supply_relation_uses_its_own_identity() -> None:
    relation = supply_relation(SUPPLY)
    assert relation is not None
    settings = supply_settings(Settings(), relation)
    assert settings.knowbase_app_service_client_id == "feed-supply"
    assert settings.knowbase_app_service_scope == "knowbase:user-data-supply"
    assert SUPPLY_PATH == "/api/internal/v2/user-data"
    # 缺一项、scope 不对、不是 https：都算没开
    for broken in (
        {k: v for k, v in SUPPLY.items() if not k.endswith("CLIENT_SECRET")},
        {**SUPPLY, "KNOWBASE_USER_DATA_SUPPLY_SERVICE_SCOPE": "knowbase:ingest"},
        {**SUPPLY, "KNOWBASE_INTERNAL_URL": "http://knowbase"},
    ):
        assert supply_relation(broken) is None

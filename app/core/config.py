from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BrowserSurface = Literal["admin", "web"]


@dataclass(frozen=True, slots=True)
class BrowserSurfaceProfile:
    """Immutable security boundary for one browser surface."""

    surface: BrowserSurface
    client_id: str
    client_secret: str
    redirect_uri: str
    application: str
    frontend_base_url: str
    frontend_origins: tuple[str, ...]
    policy_version: str
    role_allowlist: frozenset[str]
    scope_allowlist: frozenset[str]
    default_return_to: str
    allowed_return_paths: tuple[str, ...]
    required_scopes: tuple[str, ...]
    session_cookie_name: str
    transaction_cookie_name: str
    session_key_prefix: str
    transaction_key_prefix: str


class Settings(BaseSettings):
    """One App Backend configuration with two isolated browser identities."""

    env: str = "development"
    log_level: str = "INFO"
    service_name: str = "info-backend"
    deployment_id: str = "local"
    app_slug: str = "info"

    database_url: str = "postgresql+asyncpg://info:info@localhost:5432/info"
    migration_database_url: str | None = None

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_user: str | None = None
    redis_password: str | None = None

    # The provider transport is shared; each browser surface has its own
    # registered OAuth client below.
    casdoor_endpoint: str = ""
    casdoor_discovery_url: str | None = None
    casdoor_backchannel_endpoint: str | None = None
    casdoor_organization: str = "built-in"
    casdoor_verify_ssl: bool = True

    admin_casdoor_client_id: str = ""
    admin_casdoor_client_secret: str = ""
    admin_casdoor_redirect_uri: str = ""
    admin_casdoor_application: str = "sunmoonai-info-admin"
    admin_auth_policy_version: str = "info-admin-v2"
    admin_auth_role_allowlist: str = ""
    admin_auth_scope_allowlist: str = ""
    admin_frontend_base_url: str = "http://localhost:5173"
    admin_frontend_allowed_origins: str | None = None
    admin_auth_default_return_to: str = "/"
    admin_auth_allowed_return_paths: str = "/"

    web_casdoor_client_id: str = ""
    web_casdoor_client_secret: str = ""
    web_casdoor_redirect_uri: str = ""
    web_casdoor_application: str = "sunmoonai-info-web"
    web_auth_policy_version: str = "info-web-v2"
    web_auth_role_allowlist: str = ""
    web_auth_scope_allowlist: str = ""
    web_frontend_base_url: str = "http://localhost:3000"
    web_frontend_allowed_origins: str | None = None
    web_auth_default_return_to: str = "/zh-CN/dashboard"
    web_auth_allowed_return_paths: str = (
        "/zh-CN/dashboard,/en/dashboard,/zh-CN/login,/en/login"
    )
    web_frontend_default_locale: str = "zh-CN"

    auth_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    auth_transaction_ttl_seconds: int = Field(default=300, ge=60, le=900)
    auth_discovery_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_jwks_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    auth_allowed_algorithms: str = "RS256,ES256"
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    session_cookie_secure: bool | None = None

    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    reference_interaction_enabled: bool = False

    # Cross-App/provider boundary only. A same-Backend URL is rejected.
    downstream_base_url: str = ""
    downstream_client_id: str = ""
    downstream_client_secret: str = ""
    downstream_scope: str = ""
    downstream_allowed_path_prefixes: str = "/api/internal/v1"
    downstream_verify_ssl: bool = True
    downstream_http_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    # Provider-signed workload identity accepted by /api/internal dependencies.
    # The JSON object maps an exact token subject to its maximum allowed scopes.
    service_auth_audience: str = ""
    service_auth_subject_bindings_json: str = "{}"

    celery_broker_url: str | None = Field(
        default=None, validation_alias="CELERY_BROKER_URL"
    )
    celery_queue: str = Field(
        default="info.default",
        validation_alias=AliasChoices("CELERY_QUEUE", "CELERY_TASK_DEFAULT_QUEUE"),
    )
    celery_result_backend: str | None = Field(
        default=None, validation_alias="CELERY_RESULT_BACKEND"
    )

    # Info domain: durable distribution outbox.
    delivery_outbox_batch_size: int = Field(
        default=50, ge=1, le=1000, validation_alias="DELIVERY_OUTBOX_BATCH_SIZE"
    )
    delivery_outbox_lease_seconds: int = Field(
        default=30, ge=1, validation_alias="DELIVERY_OUTBOX_LEASE_SECONDS"
    )
    delivery_outbox_ack_timeout_seconds: int = Field(
        default=300, ge=1, validation_alias="DELIVERY_OUTBOX_ACK_TIMEOUT_SECONDS"
    )
    delivery_outbox_retry_base_seconds: int = Field(
        default=5, ge=1, validation_alias="DELIVERY_OUTBOX_RETRY_BASE_SECONDS"
    )
    delivery_outbox_retry_max_seconds: int = Field(
        default=300, ge=1, validation_alias="DELIVERY_OUTBOX_RETRY_MAX_SECONDS"
    )

    # Info domain: original-document object storage.
    storage_backend: str = Field(default="local", validation_alias="STORAGE_BACKEND")
    storage_local_root: str = Field(
        default=".local-storage/info-originals",
        validation_alias="STORAGE_LOCAL_ROOT",
    )
    s3_endpoint: str | None = Field(default=None, validation_alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")
    s3_access_key_id: str | None = Field(
        default=None, validation_alias="S3_ACCESS_KEY_ID"
    )
    s3_secret_access_key: str | None = Field(
        default=None, validation_alias="S3_SECRET_ACCESS_KEY"
    )
    s3_bucket: str = Field(
        default="development-info-originals", validation_alias="S3_BUCKET"
    )
    s3_force_path_style: bool = Field(
        default=True, validation_alias="S3_FORCE_PATH_STYLE"
    )
    s3_use_tls: bool = Field(default=False, validation_alias="S3_USE_TLS")

    # Info domain: crawler.
    crawl_timeout_seconds: float = Field(
        default=20.0, validation_alias="CRAWL_TIMEOUT_SECONDS"
    )
    crawl_max_bytes: int = Field(
        default=10 * 1024 * 1024, validation_alias="CRAWL_MAX_BYTES"
    )
    crawl_user_agent: str = Field(
        default="SunmoonAI InfoAppBot/0.1",
        validation_alias="CRAWL_USER_AGENT",
    )

    # Info domain: rebuildable Elasticsearch/OpenSearch read model.
    search_backend: str = Field(default="disabled", validation_alias="SEARCH_BACKEND")
    elasticsearch_url: str | None = Field(
        default=None, validation_alias="ELASTICSEARCH_URL"
    )
    elasticsearch_index: str = Field(
        default="info-information", validation_alias="ELASTICSEARCH_INDEX"
    )
    elasticsearch_timeout_seconds: float = Field(
        default=10.0, validation_alias="ELASTICSEARCH_TIMEOUT_SECONDS"
    )
    elasticsearch_username: str | None = Field(
        default=None, validation_alias="ELASTICSEARCH_USERNAME"
    )
    elasticsearch_password: str | None = Field(
        default=None, validation_alias="ELASTICSEARCH_PASSWORD"
    )
    elasticsearch_ca_cert_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ELASTICSEARCH_CA_CERT_PATH", "ELASTICSEARCH_CA_CERT"
        ),
    )
    elasticsearch_aliases: str | None = Field(
        default=None, validation_alias="ELASTICSEARCH_ALIASES"
    )

    # Info -> Knowledge workload identity and ingestion relation.
    knowledge_app_ingest_url: str | None = Field(
        default=None, validation_alias="KNOWLEDGE_APP_INGEST_URL"
    )
    knowledge_app_service_application: str = Field(
        default="sunmoonai-info-knowledge-ingest",
        validation_alias="KNOWLEDGE_APP_SERVICE_APPLICATION",
    )
    knowledge_app_service_discovery_url: str | None = Field(
        default=None,
        validation_alias="KNOWLEDGE_APP_SERVICE_DISCOVERY_URL",
    )
    knowledge_app_service_backchannel_endpoint: str | None = Field(
        default=None,
        validation_alias="KNOWLEDGE_APP_SERVICE_BACKCHANNEL_ENDPOINT",
    )
    knowledge_app_service_client_id: str | None = Field(
        default=None, validation_alias="KNOWLEDGE_APP_SERVICE_CLIENT_ID"
    )
    knowledge_app_service_client_secret: str | None = Field(
        default=None, validation_alias="KNOWLEDGE_APP_SERVICE_CLIENT_SECRET"
    )
    knowledge_app_service_scope: str = Field(
        default="knowledge:ingest", validation_alias="KNOWLEDGE_APP_SERVICE_SCOPE"
    )
    knowledge_app_timeout_seconds: float = Field(
        default=20.0, validation_alias="KNOWLEDGE_APP_TIMEOUT_SECONDS"
    )

    @field_validator("database_url", "migration_database_url", mode="before")
    @classmethod
    def normalize_postgres_url(cls, value: str | None) -> str | None:
        if not isinstance(value, str) or not (
            value.startswith("postgresql://")
            or value.startswith("postgresql+asyncpg://")
        ):
            return value
        normalized = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        parts = urlsplit(normalized)
        query = [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key != "sslmode"
        ]
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    @model_validator(mode="after")
    def validate_base_security(self) -> Settings:
        if not self.deployment_id.strip():
            raise ValueError("DEPLOYMENT_ID cannot be empty")
        if not self.app_slug or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for char in self.app_slug
        ):
            raise ValueError("APP_SLUG must use lowercase letters, digits and hyphens")
        if any(item == "*" for item in self._split_csv(self.allowed_hosts)):
            if self.is_production:
                raise ValueError("ALLOWED_HOSTS cannot use wildcard in production")
        if not self.allowed_host_list:
            raise ValueError("ALLOWED_HOSTS cannot be empty")
        if self.is_production and not self.casdoor_verify_ssl:
            raise ValueError("CASDOOR_VERIFY_SSL must be true in production")
        if self.is_production and self.session_cookie_secure is False:
            raise ValueError("SESSION_COOKIE_SECURE cannot be false in production")
        if self.is_production and self.reference_interaction_enabled:
            raise ValueError(
                "REFERENCE_INTERACTION_ENABLED cannot be true in production"
            )
        for surface in ("admin", "web"):
            profile = self.browser_profile(surface)
            if self.is_production and any(
                urlsplit(origin).scheme != "https"
                for origin in profile.frontend_origins
            ):
                raise ValueError(
                    f"{surface.upper()}_FRONTEND_ALLOWED_ORIGINS must use HTTPS "
                    "in production"
                )
        if not self.web_frontend_default_locale or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in self.web_frontend_default_locale
        ):
            raise ValueError("WEB_FRONTEND_DEFAULT_LOCALE is invalid")
        _ = self.auth_allowed_algorithm_list
        return self

    @staticmethod
    def _split_csv(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @staticmethod
    def _strict_origin(value: str, *, field: str) -> str:
        if value == "*":
            raise ValueError(f"{field} cannot contain wildcard origin")
        try:
            parsed = urlsplit(value)
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError as exc:
            raise ValueError(f"{field} contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{field} must contain origin-only HTTP(S) URLs")
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    @staticmethod
    def _validate_relative_path(value: str, *, field: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or "\x00" in value
            or len(value) > 1024
        ):
            raise ValueError(f"{field} must be a safe relative path")
        return value

    @staticmethod
    def _path_is_allowed(value: str, allowed: str) -> bool:
        path = urlsplit(value).path
        if allowed == "/":
            return path.startswith("/")
        return path == allowed or path.startswith(f"{allowed.rstrip('/')}/")

    @property
    def is_production(self) -> bool:
        return self.env not in {"development", "test"}

    @property
    def migration_url(self) -> str:
        return self.migration_database_url or self.database_url

    @property
    def casdoor_discovery_endpoint(self) -> str:
        if self.casdoor_discovery_url:
            return self.casdoor_discovery_url
        if not self.casdoor_endpoint:
            return ""
        return f"{self.casdoor_endpoint.rstrip('/')}/.well-known/openid-configuration"

    @property
    def auth_allowed_algorithm_list(self) -> tuple[str, ...]:
        values = self._split_csv(self.auth_allowed_algorithms)
        if not values:
            raise ValueError("AUTH_ALLOWED_ALGORITHMS cannot be empty")
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if set(values) - allowed:
            raise ValueError(
                "AUTH_ALLOWED_ALGORITHMS must contain only asymmetric algorithms"
            )
        return values

    @property
    def allowed_host_list(self) -> tuple[str, ...]:
        return self._split_csv(self.allowed_hosts)

    @property
    def auth_cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    def browser_profile(self, surface: BrowserSurface) -> BrowserSurfaceProfile:
        prefix = surface
        frontend_base = self._strict_origin(
            str(getattr(self, f"{prefix}_frontend_base_url")),
            field=f"{prefix.upper()}_FRONTEND_BASE_URL",
        )
        raw_origins = (
            getattr(self, f"{prefix}_frontend_allowed_origins") or frontend_base
        )
        origins = tuple(
            dict.fromkeys(
                self._strict_origin(
                    item,
                    field=f"{prefix.upper()}_FRONTEND_ALLOWED_ORIGINS",
                )
                for item in self._split_csv(raw_origins)
            )
        )
        if not origins:
            raise ValueError(
                f"{prefix.upper()}_FRONTEND_ALLOWED_ORIGINS cannot be empty"
            )
        default_return_to = self._validate_relative_path(
            str(getattr(self, f"{prefix}_auth_default_return_to")),
            field=f"{prefix.upper()}_AUTH_DEFAULT_RETURN_TO",
        )
        allowed_return_paths = tuple(
            self._validate_relative_path(
                item,
                field=f"{prefix.upper()}_AUTH_ALLOWED_RETURN_PATHS",
            )
            for item in self._split_csv(
                str(getattr(self, f"{prefix}_auth_allowed_return_paths"))
            )
        )
        if not allowed_return_paths or not any(
            self._path_is_allowed(default_return_to, allowed)
            for allowed in allowed_return_paths
        ):
            raise ValueError(
                f"{prefix.upper()}_AUTH_DEFAULT_RETURN_TO must match "
                f"{prefix.upper()}_AUTH_ALLOWED_RETURN_PATHS"
            )
        return BrowserSurfaceProfile(
            surface=surface,
            client_id=str(getattr(self, f"{prefix}_casdoor_client_id")),
            client_secret=str(getattr(self, f"{prefix}_casdoor_client_secret")),
            redirect_uri=str(getattr(self, f"{prefix}_casdoor_redirect_uri")),
            application=str(getattr(self, f"{prefix}_casdoor_application")),
            frontend_base_url=frontend_base,
            frontend_origins=origins,
            policy_version=str(getattr(self, f"{prefix}_auth_policy_version")),
            role_allowlist=frozenset(
                self._split_csv(str(getattr(self, f"{prefix}_auth_role_allowlist")))
            ),
            scope_allowlist=frozenset(
                self._split_csv(str(getattr(self, f"{prefix}_auth_scope_allowlist")))
            ),
            default_return_to=default_return_to,
            allowed_return_paths=allowed_return_paths,
            required_scopes=(f"{self.app_slug}:admin",) if surface == "admin" else (),
            session_cookie_name=f"sunmoonai_{self.app_slug}_{surface}_sid",
            transaction_cookie_name=f"sunmoonai_{self.app_slug}_{surface}_oidc_tx",
            session_key_prefix=f"{self.app_slug}:auth:{surface}:session:",
            transaction_key_prefix=f"{self.app_slug}:auth:{surface}:oidc:",
        )

    @property
    def frontend_origin_list(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.browser_profile("admin").frontend_origins,
                    *self.browser_profile("web").frontend_origins,
                )
            )
        )

    def require_browser_identity(self, surface: BrowserSurface | None = None) -> None:
        surfaces: tuple[BrowserSurface, ...] = (
            (surface,) if surface is not None else ("admin", "web")
        )
        provider_origin = self._strict_origin(
            self.casdoor_endpoint, field="CASDOOR_ENDPOINT"
        )
        discovery = urlsplit(self.casdoor_discovery_endpoint)
        if (
            discovery.scheme not in {"http", "https"}
            or not discovery.hostname
            or discovery.username
            or discovery.password
            or discovery.query
            or discovery.fragment
            or discovery.path != "/.well-known/openid-configuration"
        ):
            raise ValueError(
                "CASDOOR_DISCOVERY_URL must use the standard discovery path"
            )
        discovery_origin = f"{discovery.scheme}://{discovery.hostname}" + (
            f":{discovery.port}" if discovery.port is not None else ""
        )
        if discovery_origin != provider_origin:
            raise ValueError(
                "CASDOOR_DISCOVERY_URL must use the CASDOOR_ENDPOINT origin"
            )
        if self.is_production and urlsplit(provider_origin).scheme != "https":
            raise ValueError("CASDOOR_ENDPOINT must use HTTPS in production")

        for item in surfaces:
            profile = self.browser_profile(item)
            required = {
                f"{item.upper()}_CASDOOR_CLIENT_ID": profile.client_id,
                f"{item.upper()}_CASDOOR_CLIENT_SECRET": profile.client_secret,
                f"{item.upper()}_CASDOOR_REDIRECT_URI": profile.redirect_uri,
                f"{item.upper()}_FRONTEND_BASE_URL": profile.frontend_base_url,
            }
            missing = sorted(
                name for name, value in required.items() if not value.strip()
            )
            if missing:
                raise ValueError(
                    f"browser identity configuration missing: {', '.join(missing)}"
                )
            redirect = urlsplit(profile.redirect_uri)
            expected_path = f"/api/auth/{item}/callback"
            if (
                redirect.scheme not in {"http", "https"}
                or not redirect.hostname
                or redirect.username
                or redirect.password
                or redirect.query
                or redirect.fragment
                or redirect.path != expected_path
            ):
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must end at {expected_path}"
                )
            redirect_origin = f"{redirect.scheme}://{redirect.hostname}" + (
                f":{redirect.port}" if redirect.port is not None else ""
            )
            if redirect_origin != profile.frontend_base_url:
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must use the matching "
                    "FRONTEND_BASE_URL origin"
                )
            if profile.frontend_base_url not in profile.frontend_origins:
                raise ValueError(
                    f"{item.upper()}_FRONTEND_ALLOWED_ORIGINS must include "
                    "FRONTEND_BASE_URL"
                )
            if self.is_production and redirect.scheme != "https":
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must use HTTPS in production"
                )

    @property
    def downstream_allowed_path_prefix_list(self) -> tuple[str, ...]:
        return tuple(
            self._validate_relative_path(
                item, field="DOWNSTREAM_ALLOWED_PATH_PREFIXES"
            ).rstrip("/")
            for item in self._split_csv(self.downstream_allowed_path_prefixes)
        )

    def require_downstream_identity(self) -> None:
        required = {
            "DOWNSTREAM_BASE_URL": self.downstream_base_url,
            "DOWNSTREAM_CLIENT_ID": self.downstream_client_id,
            "DOWNSTREAM_CLIENT_SECRET": self.downstream_client_secret,
            "DOWNSTREAM_SCOPE": self.downstream_scope,
        }
        missing = sorted(name for name, value in required.items() if not value.strip())
        if missing:
            raise ValueError(
                f"downstream service configuration missing: {', '.join(missing)}"
            )
        downstream_origin = self._strict_origin(
            self.downstream_base_url, field="DOWNSTREAM_BASE_URL"
        )
        local_origins = {
            self.browser_profile("admin").frontend_base_url,
            self.browser_profile("web").frontend_base_url,
        }
        if downstream_origin in local_origins:
            raise ValueError("DOWNSTREAM_BASE_URL cannot target this Backend")
        if not self.downstream_allowed_path_prefix_list:
            raise ValueError("DOWNSTREAM_ALLOWED_PATH_PREFIXES cannot be empty")
        if self.is_production and not self.downstream_verify_ssl:
            raise ValueError("DOWNSTREAM_VERIFY_SSL must be true in production")

    @property
    def service_auth_subject_bindings(self) -> dict[str, frozenset[str]]:
        try:
            raw = json.loads(self.service_auth_subject_bindings_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "SERVICE_AUTH_SUBJECT_BINDINGS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON must be a JSON object")
        result: dict[str, frozenset[str]] = {}
        for subject, scopes in raw.items():
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 512
                or not isinstance(scopes, list)
            ):
                raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON is invalid")
            normalized = frozenset(
                item.strip()
                for item in scopes
                if isinstance(item, str) and item.strip()
            )
            if len(normalized) != len(scopes) or any(
                len(item) > 128 for item in normalized
            ):
                raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON is invalid")
            result[subject.strip()] = normalized
        return result

    def require_service_identity(self) -> None:
        if not self.service_auth_audience.strip():
            raise ValueError("SERVICE_AUTH_AUDIENCE is required")
        if not self.service_auth_subject_bindings:
            raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON cannot be empty")
        if not self.casdoor_discovery_endpoint:
            raise ValueError("CASDOOR discovery is required for service identity")

    @property
    def celery_enabled(self) -> bool:
        return bool(self.celery_broker_url)

    @property
    def search_enabled(self) -> bool:
        return self.search_backend.lower() in {"elasticsearch", "opensearch"} and bool(
            self.elasticsearch_url
        )

    @property
    def knowledge_app_ingest_enabled(self) -> bool:
        return bool(
            self.knowledge_app_ingest_url
            and self.knowledge_app_service_client_id
            and self.knowledge_app_service_client_secret
        )

    @property
    def elasticsearch_write_target(self) -> str:
        if not self.elasticsearch_aliases:
            return self.elasticsearch_index
        try:
            aliases = json.loads(self.elasticsearch_aliases)
        except json.JSONDecodeError:
            return self.elasticsearch_index
        information = aliases.get("information")
        if isinstance(information, dict) and information.get("write"):
            return str(information["write"])
        return self.elasticsearch_index

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

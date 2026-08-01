from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration shared by the API and optional Celery worker.

    Browser identity settings are validated explicitly by
    :meth:`require_browser_identity` so a worker can start without receiving a
    browser client secret. The API calls that method before opening external
    connections.
    """

    env: str = "development"
    log_level: str = "INFO"
    service_name: str = "info-admin-backend"
    app_slug: str = "info"
    surface: str = "admin"

    database_url: str = "postgresql+asyncpg://info:info@localhost:5432/info"
    migration_database_url: str | None = None

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_user: str | None = None
    redis_password: str | None = None

    casdoor_endpoint: str = ""
    casdoor_discovery_url: str | None = None
    casdoor_backchannel_endpoint: str | None = None
    casdoor_client_id: str = ""
    casdoor_client_secret: str = ""
    casdoor_redirect_uri: str = ""
    casdoor_organization: str = "built-in"
    casdoor_application: str = "sunmoonai-info-admin"
    casdoor_verify_ssl: bool = True

    auth_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    auth_transaction_ttl_seconds: int = Field(default=300, ge=60, le=900)
    auth_discovery_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_jwks_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    auth_allowed_algorithms: str = "RS256,ES256"
    auth_policy_version: str = "info-admin-v1"
    auth_role_allowlist: str = ""
    auth_scope_allowlist: str = ""

    frontend_base_url: str = "http://localhost:5173"
    frontend_allowed_origins: str | None = None
    allowed_hosts: str = "localhost,127.0.0.1,testserver"

    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    session_cookie_secure: bool | None = None

    celery_broker_url: str | None = Field(
        default=None, validation_alias="CELERY_BROKER_URL"
    )
    celery_queue: str = Field(
        default="info.admin.default",
        validation_alias=AliasChoices("CELERY_QUEUE", "CELERY_TASK_DEFAULT_QUEUE"),
    )
    celery_result_backend: str | None = Field(
        default=None, validation_alias="CELERY_RESULT_BACKEND"
    )

    # Durable distribution outbox.
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

    # Info App object storage. S3 is the production contract; local storage keeps
    # the first development loop runnable before platform S3 credentials exist.
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

    # Optional Elasticsearch/OpenSearch index used as a rebuildable read model.
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

    # Optional Knowledge App ingestion endpoint.
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
        if self.surface != "admin":
            raise ValueError("info-admin-backend requires SURFACE=admin")
        if not self.app_slug or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for char in self.app_slug
        ):
            raise ValueError("APP_SLUG must use lowercase letters, digits and hyphens")
        origins = tuple(
            self._strict_origin(item, field="FRONTEND_ALLOWED_ORIGINS")
            for item in self._split_csv(self.frontend_origins_raw)
        )
        if not origins:
            raise ValueError(
                "FRONTEND_ALLOWED_ORIGINS must contain an absolute HTTP(S) origin"
            )
        if self.is_production and any(
            urlsplit(origin).scheme != "https" for origin in origins
        ):
            raise ValueError("FRONTEND_ALLOWED_ORIGINS must use HTTPS in production")
        if any(item.strip() == "*" for item in self._split_csv(self.allowed_hosts)):
            if self.is_production:
                raise ValueError("ALLOWED_HOSTS cannot use wildcard in production")
        if self.is_production and not self.casdoor_verify_ssl:
            raise ValueError("CASDOOR_VERIFY_SSL must be true in production")
        if self.is_production and self.session_cookie_secure is False:
            raise ValueError("SESSION_COOKIE_SECURE cannot be false in production")
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
    def frontend_origins_raw(self) -> str:
        return self.frontend_allowed_origins or self.frontend_base_url

    @property
    def frontend_origin_list(self) -> tuple[str, ...]:
        origins = [
            self._strict_origin(item, field="FRONTEND_ALLOWED_ORIGINS")
            for item in self._split_csv(self.frontend_origins_raw)
        ]
        return tuple(dict.fromkeys(origins))

    @property
    def auth_role_allowlist_items(self) -> frozenset[str]:
        return frozenset(self._split_csv(self.auth_role_allowlist))

    @property
    def auth_scope_allowlist_items(self) -> frozenset[str]:
        return frozenset(self._split_csv(self.auth_scope_allowlist))

    @property
    def allowed_host_list(self) -> tuple[str, ...]:
        return self._split_csv(self.allowed_hosts)

    @property
    def auth_cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    @property
    def session_cookie_name(self) -> str:
        return f"sunmoonai_{self.app_slug}_{self.surface}_sid"

    @property
    def transaction_cookie_name(self) -> str:
        return f"sunmoonai_{self.app_slug}_{self.surface}_oidc_tx"

    @property
    def session_key_prefix(self) -> str:
        return f"{self.app_slug}:auth:{self.surface}:session:"

    @property
    def transaction_key_prefix(self) -> str:
        return f"{self.app_slug}:auth:{self.surface}:oidc:"

    @property
    def required_admin_scope(self) -> str:
        return f"{self.app_slug}:admin"

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


    def require_browser_identity(self) -> None:
        required = {
            "CASDOOR_ENDPOINT": self.casdoor_endpoint,
            "CASDOOR_CLIENT_ID": self.casdoor_client_id,
            "CASDOOR_CLIENT_SECRET": self.casdoor_client_secret,
            "CASDOOR_REDIRECT_URI": self.casdoor_redirect_uri,
            "FRONTEND_BASE_URL": self.frontend_base_url,
        }
        missing = sorted(name for name, value in required.items() if not value.strip())
        if missing:
            raise ValueError(
                f"browser identity configuration missing: {', '.join(missing)}"
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

        redirect = urlsplit(self.casdoor_redirect_uri)
        if (
            redirect.scheme not in {"http", "https"}
            or not redirect.hostname
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
            or redirect.path != "/api/auth/callback"
        ):
            raise ValueError("CASDOOR_REDIRECT_URI must end at /api/auth/callback")
        if self.is_production and redirect.scheme != "https":
            raise ValueError("CASDOOR_REDIRECT_URI must use HTTPS in production")
        frontend_origin = self._strict_origin(
            self.frontend_base_url, field="FRONTEND_BASE_URL"
        )
        if self.is_production and urlsplit(frontend_origin).scheme != "https":
            raise ValueError("FRONTEND_BASE_URL must use HTTPS in production")
        if (
            f"{redirect.scheme}://{redirect.hostname}"
            + (f":{redirect.port}" if redirect.port is not None else "")
            != frontend_origin
        ):
            raise ValueError(
                "CASDOOR_REDIRECT_URI must use the FRONTEND_BASE_URL origin"
            )
        if not self.frontend_origin_list:
            raise ValueError("FRONTEND_ALLOWED_ORIGINS must contain an absolute origin")
        if frontend_origin not in self.frontend_origin_list:
            raise ValueError("FRONTEND_ALLOWED_ORIGINS must include FRONTEND_BASE_URL")
        if not self.allowed_host_list:
            raise ValueError("ALLOWED_HOSTS cannot be empty")
        # Force algorithm validation during startup rather than first callback.
        _ = self.auth_allowed_algorithm_list

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

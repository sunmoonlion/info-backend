from functools import lru_cache
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 基础配置
    env: str = "development"
    log_level: str = "INFO"

    # 数据库（读 DATABASE_URL，自动补 +asyncpg 驱动前缀）
    database_url: str = "postgresql+asyncpg://info:info@localhost:5432/info"
    # 可选：Alembic 迁移专用账号。运行时仍使用 DATABASE_URL。
    migration_database_url: str | None = None

    @field_validator("database_url", "migration_database_url", mode="before")
    @classmethod
    def ensure_asyncpg(cls, v: str) -> str:
        if isinstance(v, str) and (
            v.startswith("postgresql://") or v.startswith("postgresql+asyncpg://")
        ):
            url = v.replace("postgresql://", "postgresql+asyncpg://", 1)
            parts = urlsplit(url)
            query = [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
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
        return v

    # Redis（dbctl ACL 场景可设 REDIS_USER；仅 default 密码时可留空）
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_user: str | None = None
    redis_password: str | None = None

    # Casdoor BFF
    casdoor_endpoint: str = ""
    casdoor_client_id: str = ""
    casdoor_client_secret: str = ""
    casdoor_redirect_uri: str = ""
    casdoor_organization: str = "built-in"
    casdoor_application: str = "sunmoonai-info-admin"
    casdoor_discovery_url: str | None = None
    casdoor_verify_ssl: bool = True

    auth_http_timeout_seconds: float = 10.0
    auth_transaction_ttl_seconds: int = 300
    auth_discovery_cache_seconds: int = 300
    auth_jwks_cache_seconds: int = 300
    auth_clock_skew_seconds: int = 30
    auth_allowed_algorithms: str = "RS256,ES256"
    auth_policy_version: str = "info-admin-v1"
    session_cookie_secure: bool | None = None

    # Frontend
    # Used for post-login redirects from backend callback.
    frontend_base_url: str = "http://localhost:5173"
    frontend_allowed_origins: str | None = None

    # Session
    session_ttl_seconds: int = 3600

    @model_validator(mode="after")
    def validate_security_configuration(self) -> "Settings":
        raw_origins = self.frontend_allowed_origins or self.frontend_base_url
        if any(item.strip() == "*" for item in raw_origins.split(",")):
            raise ValueError("credential CORS cannot use wildcard origin")
        if self.env not in {"development", "test"}:
            if not self.casdoor_verify_ssl:
                raise ValueError("CASDOOR_VERIFY_SSL must be true in production")
            for field, value in (
                ("CASDOOR_ENDPOINT", self.casdoor_endpoint),
                ("CASDOOR_REDIRECT_URI", self.casdoor_redirect_uri),
                ("FRONTEND_BASE_URL", self.frontend_base_url),
            ):
                if value and urlsplit(value).scheme != "https":
                    raise ValueError(f"{field} must use HTTPS in production")
        return self

    @property
    def casdoor_discovery_endpoint(self) -> str:
        if self.casdoor_discovery_url:
            return self.casdoor_discovery_url
        if not self.casdoor_endpoint or not self.casdoor_application:
            return ""
        return (
            f"{self.casdoor_endpoint.rstrip('/')}/.well-known/"
            f"{self.casdoor_application}/openid-configuration"
        )

    @property
    def auth_allowed_algorithm_list(self) -> tuple[str, ...]:
        values = tuple(
            item.strip() for item in self.auth_allowed_algorithms.split(",") if item.strip()
        )
        if not values:
            raise ValueError("AUTH_ALLOWED_ALGORITHMS cannot be empty")
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        unsupported = set(values) - allowed
        if unsupported:
            raise ValueError(
                "AUTH_ALLOWED_ALGORITHMS must contain only configured asymmetric algorithms"
            )
        return values

    @property
    def frontend_origin_list(self) -> tuple[str, ...]:
        raw = self.frontend_allowed_origins or self.frontend_base_url
        values: list[str] = []
        for item in raw.split(","):
            parsed = urlsplit(item.strip())
            if not parsed.scheme or not parsed.hostname:
                continue
            port = f":{parsed.port}" if parsed.port is not None else ""
            values.append(f"{parsed.scheme}://{parsed.hostname}{port}")
        return tuple(dict.fromkeys(values))

    @property
    def auth_cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.env not in {"development", "test"}

    # Celery（应用层只读 CELERY_BROKER_URL；k8s 按 Deployment 注入 producer/worker 账号）
    celery_broker_url: str | None = Field(
        default=None, validation_alias="CELERY_BROKER_URL"
    )
    celery_queue: str = Field(
        default="default",
        validation_alias=AliasChoices("CELERY_QUEUE", "CELERY_TASK_DEFAULT_QUEUE"),
    )
    celery_result_backend: str | None = Field(
        default=None, validation_alias="CELERY_RESULT_BACKEND"
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
    s3_bucket: str = Field(default="development-info-originals", validation_alias="S3_BUCKET")
    s3_force_path_style: bool = Field(default=True, validation_alias="S3_FORCE_PATH_STYLE")
    s3_use_tls: bool = Field(default=False, validation_alias="S3_USE_TLS")

    crawl_timeout_seconds: float = Field(default=20.0, validation_alias="CRAWL_TIMEOUT_SECONDS")
    crawl_max_bytes: int = Field(default=10 * 1024 * 1024, validation_alias="CRAWL_MAX_BYTES")
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

    # Optional Knowledge App ingestion endpoint. Disabled until a public
    # knowledge-admin-backend contract is configured.
    knowledge_app_ingest_url: str | None = Field(
        default=None, validation_alias="KNOWLEDGE_APP_INGEST_URL"
    )
    knowledge_app_service_application: str = Field(
        default="sunmoonai-info-knowledge-ingest",
        validation_alias="KNOWLEDGE_APP_SERVICE_APPLICATION",
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


@lru_cache()
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 基础配置
    env: str = "development"
    log_level: str = "INFO"

    # 数据库（读 DATABASE_URL，自动补 +asyncpg 驱动前缀）
    database_url: str = "postgresql+asyncpg://info:info@localhost:5432/info"

    @field_validator("database_url", mode="before")
    @classmethod
    def ensure_asyncpg(cls, v: str) -> str:
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
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
    casdoor_application: str = "app-info"
    casdoor_verify_ssl: bool = True

    # Frontend
    # Used for post-login redirects from backend callback.
    frontend_base_url: str = "http://localhost:5173"

    # Session
    session_ttl_seconds: int = 3600

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

    @property
    def celery_enabled(self) -> bool:
        return bool(self.celery_broker_url)

    @property
    def search_enabled(self) -> bool:
        return self.search_backend.lower() in {"elasticsearch", "opensearch"} and bool(
            self.elasticsearch_url
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()

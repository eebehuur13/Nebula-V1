"""Settings, read from NEBULA_* environment variables.

Everything that differs between a laptop and AWS lives here. Secrets arrive as
environment variables too; on AWS, ECS injects them from Secrets Manager.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEBULA_", env_file=".env", extra="ignore")

    # Core
    env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql://nebula:nebula@localhost:5433/nebula"
    # On AWS the database secret is injected as separate fields; if db_host is set
    # these win over database_url.
    db_host: str = ""
    db_port: int = 5432
    db_user: str = ""
    db_password: str = ""
    db_name: str = "nebula"
    public_url: str = "http://localhost:8000"
    aws_region: str = "us-east-2"

    # Sign-in. "dev" skips OAuth and trusts NEBULA_DEV_USER_EMAIL (local only).
    auth_mode: Literal["google", "dev"] = "dev"
    dev_user_email: str = "dev@example.org"
    google_client_id: str = ""
    google_client_secret: str = ""
    jwt_signing_key: str = "dev-only-signing-key-change-me"
    # Public sign-in is independent of RL membership and content permissions.
    public_signup: bool = False
    # Accounts in these domains are members of the organisation ("All RL"). Set your own domain.
    allowed_email_domains: str = "example.org"
    allowed_emails: str = ""
    admin_emails: str = ""

    # Links shown in the footer of the web pages; left out when empty.
    about_url: str = ""
    privacy_url: str = ""
    terms_url: str = ""

    # Files. If s3_bucket is empty, files go to local_storage_dir instead.
    s3_bucket: str = ""
    local_storage_dir: str = ".data/files"

    # Models
    embed_provider: Literal["bedrock-cohere", "bedrock-titan", "local", "fake"] = "local"
    embed_dim: int = 1024
    local_embed_model: str = "intfloat/multilingual-e5-large"
    rerank_provider: Literal["bedrock-cohere", "local", "none"] = "none"
    local_rerank_model: str = "BAAI/bge-reranker-v2-m3-int8"

    # Source credentials (only needed for the connectors that are used)
    google_service_account_json: str = ""
    google_impersonate_email: str = ""
    notion_token: str = ""
    granola_api_key: str = ""

    # Limits that keep tool results inside Claude's limits (~150k chars per result)
    max_result_chars: int = 60_000
    search_default_limit: int = 8

    # Per-account limits, independent of the number of API replicas.
    tool_calls_per_minute: int = Field(default=120, ge=1)
    writes_per_day: int = Field(default=1000, ge=1)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    storage_bytes_per_person: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    collections_per_person: int = Field(default=20, ge=1)
    source_items_per_collection: int = Field(default=2000, ge=1)

    worker_poll_seconds: float = 2.0

    @field_validator("google_client_id", "google_client_secret", "notion_token", "granola_api_key",
                     "google_service_account_json", mode="before")
    @classmethod
    def _unset_is_empty(cls, v: object) -> object:
        # The AWS secret starts with "unset" placeholders until someone fills them in.
        return "" if isinstance(v, str) and v.strip().lower() in ("unset", "none", "-") else v

    @property
    def dsn(self) -> str:
        if self.db_host:
            from urllib.parse import quote

            return (
                f"postgresql://{quote(self.db_user)}:{quote(self.db_password)}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self.database_url

    def footer_links(self) -> str:
        """HTML links to the about, privacy and terms pages that are configured."""
        import html

        pairs = [("About", self.about_url), ("Privacy", self.privacy_url), ("Terms", self.terms_url)]
        return " · ".join(f"<a href='{html.escape(url)}'>{label}</a>" for label, url in pairs if url)

    @property
    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()}

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    def email_allowed(self, email: str) -> bool:
        email = email.strip().lower()
        if self.public_signup:
            return bool(email.count("@") == 1 and all(email.split("@")))
        if email in self.allowed_email_set or email in self.admin_email_set:
            return True
        return email.rsplit("@", 1)[-1] in self.allowed_domains


@lru_cache
def settings() -> Settings:
    return Settings()


# Re-exported for type hints elsewhere
SettingsT = Settings
__all__ = ["Settings", "settings", "Field"]

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Locate root directory containing .env file
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent


class Settings(BaseSettings):
    """Enterprise Compliance Pipeline System Configuration."""

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Enterprise Agentic Compliance Pipeline"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api"

    # Groq LLM Configuration
    GROQ_API_KEY: str = Field(default="gsk_placeholder", description="Groq API Key")
    GROQ_PLANNER_MODEL: str = "openai/gpt-oss-120b"
    GROQ_JUDGE_MODEL: str = "openai/gpt-oss-120b"
    GROQ_FAST_MODEL: str = "openai/gpt-oss-20b"

    # Dual PostgreSQL Configuration
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres_secure_password"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5434

    # Database Names
    GOVERNANCE_DB_NAME: str = "governance_db"
    STAGING_DB_NAME: str = "staging_db"

    # Async Database URLs (asyncpg)
    GOVERNANCE_DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres_secure_password@localhost:5434/governance_db"
    )
    STAGING_DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres_secure_password@localhost:5434/staging_db"
    )

    # Sync Database URL for Checkpointer (psycopg3)
    GOVERNANCE_SYNC_DATABASE_URL: str = (
        "postgresql://postgres:postgres_secure_password@localhost:5434/governance_db"
    )

    # Redis Hot Tier
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_SESSION_TTL_SECONDS: int = 86400

    # MinIO S3 Cold Tier
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_NAME: str = "agent-payloads"
    MINIO_SECURE: bool = False
    PAYLOAD_OFFLOAD_THRESHOLD_BYTES: int = 5120

    # Cryptographic HITL Security
    JWT_SECRET: str = "enterprise_super_secret_signing_key_replace_in_prod_at_least_32_chars"
    JWT_ALGORITHM: str = "HS256"
    APPROVAL_TOKEN_EXPIRE_MINUTES: int = 1440

    # FastMCP Sandbox Service
    FASTMCP_SERVICE_URL: str = "http://localhost:8001"

    # Admin Authentication Credentials
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    ADMIN_EMAIL: str = "compliance.admin@enterprise.local"
    AUTH_TOKEN_EXPIRE_MINUTES: int = 480


settings = Settings()

"""
Main FastAPI Application Entrypoint & Lifespan Configuration.

Configures:
- Application lifespan with dual PostgreSQL, Redis, and FastMCP connectivity verification.
- CORS middleware for frontend integrations.
- API router mounting under /api.
- Comprehensive system health checking endpoint (/health).
"""

from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
import httpx
import redis.asyncio as redis
from sqlalchemy import text

from backend.app.api import api_router
from backend.app.api.schemas import HealthResponse
from backend.app.core.config import settings
from backend.app.db.session import close_db_connections, gov_engine, staging_engine

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("compliance_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    Verifies database and service connectivity on startup, and cleans up resources on shutdown.
    """
    logger.info("Initializing Enterprise Agentic Compliance Gateway...")

    # 1. Verify Governance DB connectivity
    try:
        async with gov_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Connection to governance_db established successfully.")
    except Exception as ex:
        logger.error(f"Failed to connect to governance_db: {ex}")

    # 2. Verify Staging DB connectivity
    try:
        async with staging_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Connection to staging_db established successfully.")
    except Exception as ex:
        logger.error(f"Failed to connect to staging_db: {ex}")

    # 3. Verify Redis Hot Tier connectivity
    try:
        r_client = redis.from_url(settings.REDIS_URL)
        await r_client.ping()
        await r_client.aclose()
        logger.info("Connection to Redis hot tier established successfully.")
    except Exception as ex:
        logger.warning(f"Failed to connect to Redis hot tier: {ex}")

    # 4. Verify FastMCP Sandbox connectivity
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.FASTMCP_SERVICE_URL}/health")
            if resp.status_code == 200:
                logger.info("FastMCP sandbox microservice is healthy.")
            else:
                logger.warning(f"FastMCP sandbox returned status {resp.status_code}.")
    except Exception as ex:
        logger.warning(f"Could not reach FastMCP sandbox service: {ex}")

    yield

    # Shutdown
    logger.info("Shutting down Compliance Gateway; closing database connection pools...")
    await close_db_connections()
    logger.info("Database connection pools disposed.")


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Enterprise Multi-Agent Compliance & Governed Data Cleaning API. "
        "Orchestrates PII sanitization (Presidio), LLM SQL synthesis (Groq), "
        "LLM-as-a-Judge semantic safety evaluation, cryptographic HMAC-SHA256 HITL authorization, "
        "and isolated transactional execution with row pre-image rollbacks (FastMCP)."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all API routers under settings.API_V1_PREFIX (/api)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["Root"])
async def root():
    """Root metadata and API status."""
    return {
        "service": settings.APP_NAME,
        "version": "1.0.0",
        "environment": settings.APP_ENV,
        "docs_url": "/docs",
        "health_check": "/health",
        "api_v1_prefix": settings.API_V1_PREFIX,
    }


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Root"],
    summary="Comprehensive system health check across dual DBs, Redis, and FastMCP",
)
async def health_check() -> HealthResponse:
    """
    Checks real-time connectivity against:
    - PostgreSQL governance_db
    - PostgreSQL staging_db
    - Redis Hot Tier
    - FastMCP Sandbox Microservice
    """
    gov_status = "HEALTHY"
    staging_status = "HEALTHY"
    redis_status = "HEALTHY"
    fastmcp_status = "HEALTHY"

    try:
        async with gov_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        gov_status = "UNHEALTHY"

    try:
        async with staging_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        staging_status = "UNHEALTHY"

    try:
        r = redis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
    except Exception:
        redis_status = "UNHEALTHY"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.FASTMCP_SERVICE_URL}/health")
            if resp.status_code != 200:
                fastmcp_status = "DEGRADED"
    except Exception:
        fastmcp_status = "UNHEALTHY"

    overall_status = (
        "HEALTHY"
        if all(s == "HEALTHY" for s in [gov_status, staging_status, redis_status, fastmcp_status])
        else "DEGRADED"
    )

    return HealthResponse(
        status=overall_status,
        service=settings.APP_NAME,
        environment=settings.APP_ENV,
        postgres_governance_db=gov_status,
        postgres_staging_db=staging_status,
        redis=redis_status,
        fastmcp_sandbox=fastmcp_status,
    )

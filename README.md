# Data Cleaner AI Agent — Backend & Infrastructure

Enterprise-grade autonomous data cleaning and governance backend powered by FastAPI, LangGraph multi-agent architecture, FastMCP execution sandbox, Neon Serverless PostgreSQL (dual governance + staging DBs), Upstash Redis hot cache, and Cloudflare R2 / MinIO S3 object storage.

---

## 🚀 Cloud Deployment Tech Stack

The production architecture is deployed across modern serverless and containerized cloud providers:

| Layer | Service / Provider | Purpose | Configuration / Endpoint |
|---|---|---|---|
| **Frontend UI** | **Vercel** | Next.js 15 App Router web cockpit, real-time agent SSE streaming, diff previews & rollbacks | `NEXT_PUBLIC_API_URL` pointing to Render backend |
| **Backend API** | **Render (Web Service)** | FastAPI gateway, LangGraph orchestration, cryptographic HITL gate, audit logging | `https://data-cleaner-ai-agent-backend.onrender.com` |
| **Sandbox Execution** | **Render / Docker** | Air-gapped **FastMCP** execution engine capturing ACID row pre-images and enforcing AST safety | `https://data-cleaner-fastmcp.onrender.com` (or internal service) |
| **Governance DB** | **Neon (PostgreSQL)** | Serverless DB storing `supervisor_threads`, `execution_plans`, `agent_executions`, pre-images & audit trail | `GOVERNANCE_DATABASE_URL` (`ep-round-boat-...pooler.neon.tech`) |
| **Staging CRM DB** | **Neon (PostgreSQL)** | Serverless DB holding target dirty enterprise data (`clients`, `invoices`, GDPR consent) | `STAGING_DATABASE_URL` (`ep-shiny-star-...pooler.neon.tech`) |
| **Hot Cache Tier** | **Upstash Redis** | Serverless TLS-encrypted Redis for agent session state, step counters, and distributed locks | `REDIS_URL` (`rediss://default:...@...upstash.io:6379`) |
| **Cold Storage Tier**| **Cloudflare R2 / MinIO**| S3-compatible object storage for execution reports and payload dumps exceeding 5 KB | `MINIO_ENDPOINT`, `MINIO_BUCKET_NAME="agent-payloads"` |
| **LLM Inference** | **Groq Cloud** | High-speed LLM inference (`qwen/qwen3.8-27b`) with guaranteed JSON schema output | `GROQ_API_KEY` |
| **Observability** | **LangSmith** | Distributed multi-agent tracing, latency tracking, LLM input/output token telemetry | `LANGCHAIN_TRACING_V2="true"`, `LANGCHAIN_API_KEY` |

---

## 🏗️ Architecture Overview

```mermaid
flowchart TD
    User([Compliance Officer / User]) -->|Browser| Frontend[Next.js Frontend on Vercel]
    Frontend -->|REST API / SSE Streams| Backend[FastAPI Backend on Render]
    
    subgraph MultiAgent [LangGraph Multi-Agent Cluster]
        Guardrail[Guardrail Agent\nPresidio PII Masking] --> Planner[Compliance Planner\nGroq LLM + MCP Schema]
        Planner --> Judge[Safety Judge Agent\nAST & Semantic Security]
        Judge --> HITL{Cryptographic HITL Gate\nHMAC-SHA256 Token}
        HITL -->|Approved| Worker[Execution Worker\nPII Unmasking]
    end

    Backend --> MultiAgent
    Worker -->|Execute Mutation / Dry-run| FastMCP[FastMCP Sandbox Microservice]
    
    FastMCP -->|ACID Pre-images / Audit Logs| GovDB[(Neon PostgreSQL\nGovernance DB)]
    FastMCP -->|Sanitized SQL Execution| StagingDB[(Neon PostgreSQL\nStaging CRM DB)]
    
    Backend <-->|Hot State / Locks| Upstash[(Upstash Redis Hot Tier)]
    Backend -->|Payloads > 5KB| R2[(Cloudflare R2 / MinIO Cold Tier)]
```

- **FastAPI Core (`backend/app`)**: REST API endpoints for pipeline execution, HITL approval, audit trail, CRM inspection, and deterministic rollback.
- **Multi-Agent System (`backend/app/agents`)**:
  - `GuardrailAgent`: Presidio PII detection & reversible token masking (`[MASKED_EMAIL_ADDRESS_1]`).
  - `CompliancePlanner`: Schema-aware SQL normalization synthesis with guaranteed JSON Schema via Groq.
  - `SafetyJudge`: Semantic safety verification, prohibited operation checks, and reflection loops.
  - `ExecutionWorker`: Resumes past the HITL gate upon cryptographic HMAC-SHA256 token validation.
- **FastMCP Sandbox (`fastmcp_server`)**: Air-gapped tool execution service capturing ACID pre-images before applying SQL mutations.
- **Dual Database Architecture (`governance_db` + `staging_db`)**: Full physical isolation between audit/governance records and the dirty CRM staging data.

---

## ⚙️ Cloud Environment Variables Reference

When deploying to **Render** or your cloud environment, configure the following variables:

```bash
# Application Configuration
APP_NAME="Enterprise Agentic Compliance Pipeline"
APP_ENV="production"
DEBUG="False"
PORT="8000"

# Groq LLM API
GROQ_API_KEY="gsk_..."
GROQ_PLANNER_MODEL="qwen/qwen3.8-27b"
GROQ_JUDGE_MODEL="qwen/qwen3.8-27b"
GROQ_FAST_MODEL="qwen/qwen3.8-27b"

# Dual PostgreSQL (Neon Serverless Connection Strings)
GOVERNANCE_DATABASE_URL="postgresql+asyncpg://neondb_owner:<pwd>@<ep-gov>-pooler.ap-southeast-1.aws.neon.tech/neondb?ssl=require"
STAGING_DATABASE_URL="postgresql+asyncpg://neondb_owner:<pwd>@<ep-staging>-pooler.ap-southeast-1.aws.neon.tech/neondb?ssl=require"
GOVERNANCE_SYNC_DATABASE_URL="postgresql://neondb_owner:<pwd>@<ep-gov>-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

# Upstash Redis Hot Tier
REDIS_URL="rediss://default:<pwd>@<subdomain>.upstash.io:6379"
REDIS_SESSION_TTL_SECONDS="86400"

# FastMCP Sandbox Service URL
FASTMCP_SERVICE_URL="https://data-cleaner-fastmcp.onrender.com" # or internal service URL

# Cloudflare R2 / MinIO Cold Storage Tier
MINIO_ENDPOINT="<account-id>.r2.cloudflarestorage.com"
MINIO_ACCESS_KEY="<r2-access-key-id>"
MINIO_SECRET_KEY="<r2-secret-access-key>"
MINIO_BUCKET_NAME="agent-payloads"
MINIO_SECURE="True"
PAYLOAD_OFFLOAD_THRESHOLD_BYTES="5120"

# Cryptographic HITL Security
JWT_SECRET="<generate-a-secure-random-32+-character-secret>"
JWT_ALGORITHM="HS256"
APPROVAL_TOKEN_EXPIRE_MINUTES="1440"

# LangSmith Observability & Tracing (https://smith.langchain.com)
LANGCHAIN_TRACING_V2="true"
LANGCHAIN_API_KEY="lsv2_pt_your_langsmith_api_key_here"
LANGCHAIN_PROJECT="data-cleaner-ai-agent"
LANGCHAIN_ENDPOINT="https://api.smith.langchain.com"
```

---

## 💻 Local Development Quickstart

### 1. Prerequisites
- Docker & Docker Compose
- Python 3.11 or 3.12
- Node.js 18+ (for frontend)

### 2. Environment Setup
```bash
cp .env.example .env
# Fill in your GROQ_API_KEY and database credentials
```

### 3. Start Local Microservices via Docker Compose
```bash
docker compose up -d
```
This runs:
- PostgreSQL dual database on `localhost:5434`
- Redis on `localhost:6379`
- MinIO on `localhost:9000` (Console on `:9001`)
- FastMCP sandbox on `localhost:8001`

### 4. Install Dependencies & Start FastAPI Gateway
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Run FastAPI backend
PYTHONPATH=. uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Run Test Suite
```bash
PYTHONPATH=. pytest backend/tests -v
```

# Data Cleaner AI Agent — Backend & Infrastructure

Enterprise-grade autonomous data cleaning and governance backend powered by FastAPI, LangGraph multi-agent architecture, FastMCP execution sandbox, PostgreSQL (governance + staging DBs), Redis hot cache, and MinIO S3 object storage.

---

## Architecture Overview

- **FastAPI Core (`backend/app`)**: REST API endpoints for pipeline execution, HITL approval, audit trail, CRM inspection, and deterministic rollback.
- **Multi-Agent System (`backend/app/agents`)**:
  - `PlannerAgent`: Generates step-by-step SQL mutation plans.
  - `GuardrailAgent`: SQL AST validation (prohibiting unconstrained mutations, table drops, and multi-tenant cross-contamination).
  - `ExecutionWorker`: Dispatches validated statements to FastMCP sandbox.
  - `JudgeAgent`: Evaluates execution results, data quality improvements, and safety criteria.
- **FastMCP Sandbox (`fastmcp_server`)**: Air-gapped tool execution service capturing ACID pre-images before applying SQL mutations.
- **Infrastructure (`infra` & `docker-compose.yml`)**:
  - PostgreSQL 16 dual-database (`governance_db` and `staging_db`).
  - Redis 7 (hot session cache, locks, TTL).
  - MinIO (S3 cold storage for payloads > 5KB).
  - FastMCP container.

---

## Quickstart

### 1. Prerequisites
- Docker & Docker Compose
- Python 3.11 or 3.12

### 2. Environment Setup
```bash
cp .env.example .env
# Edit .env with your GEMINI_API_KEY if using live Gemini models
```

### 3. Start Infrastructure Services
```bash
docker compose up -d
```
This starts:
- PostgreSQL on `localhost:5434`
- Redis on `localhost:6379`
- MinIO on `localhost:9000` (Console on `:9001`)
- FastMCP on `localhost:8001`

### 4. Install Backend Dependencies & Run API
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Run FastAPI backend
PYTHONPATH=. uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Running Test Suite
```bash
pytest backend/tests -v
```

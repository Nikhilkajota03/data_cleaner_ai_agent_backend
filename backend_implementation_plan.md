# Enterprise Agentic Compliance Pipeline: Backend Implementation Plan

This implementation plan breaks down the backend construction into small, modular, reviewable steps. Each step defines the precise files to be created, the technical objectives, and the exact verification criteria.

---

## Phase 1: Infrastructure, Configuration & Data Seeding

### Step 1: Project Scaffolding & Environment Configuration [COMPLETED]
- **Objective:** Establish the foundational directory layout, environment configuration templates, and Python dependency definitions.
- **Files Created:**
  - `.env.example`
  - `.gitignore`
  - `backend/requirements.txt`
  - `backend/app/core/config.py` (Pydantic Settings for DB, Redis, Groq, MinIO, and JWT secrets)
- **Verification:** Execute `python -c "from backend.app.core.config import settings; print(settings.APP_NAME)"` to confirm environment variables parse into typed Pydantic models. (Verified: `APP: Enterprise Agentic Compliance Pipeline | ENV: development | GOV_DB: governance_db | STAGING_DB: staging_db`)

### Step 2: Database Initialization & Seeding Scripts [COMPLETED]
- **Objective:** Write raw SQL scripts for dual-database provisioning, audit schema creation, and dirty CRM data generation.
- **Files Created:**
  - `infra/postgres/01_init_databases.sql` (Creates `governance_db` and `staging_db`)
  - `infra/postgres/02_governance_schema.sql` (Enums, tables: `supervisor_threads`, `message_history`, `execution_plans`, `agent_executions`, `tool_logs`, `row_pre_images`)
  - `infra/postgres/03_staging_crm_seed.sql` (Messy EU client records, malformed phone numbers, unstandardized country codes, missing GDPR consent flags)
- **Verification:** Lint SQL scripts for syntax and foreign key integrity. (Verified: all 3 scripts parsed without syntax errors via `sqlglot`)

### Step 3: Containerized Infrastructure Deployment [COMPLETED]
- **Objective:** Deploy local storage and cache infrastructure using Docker Compose.
- **Files Created:**
  - `docker-compose.yml` (PostgreSQL 16 on port 5434 with auto-init volume, Redis 7 on 6379, MinIO on 9000/9001)
- **Verification:** Run `docker compose up -d postgres redis minio`, then verify with `docker compose ps` and `psql` queries against both `governance_db` and `staging_db`. (Verified: all 3 containers healthy; `governance_db` contains 6 tables; `staging_db` contains seeded CRM clients and invoices)

---

## Phase 2: Core Persistence, Storage & Security Services

### Step 4: Asynchronous Database Sessions & ORM Models [COMPLETED]
- **Objective:** Implement non-blocking database connections for both `governance_db` and `staging_db` using SQLAlchemy and `asyncpg`.
- **Files Created:**
  - `backend/app/db/__init__.py`
  - `backend/app/db/session.py` (Dual engine factory: `get_gov_db()` and `get_staging_db()`)
  - `backend/app/db/models.py` (SQLAlchemy 2.0 mapped models for all governance & CRM tables)
- **Verification:** Run an async test query selecting tables from `information_schema` on both databases. (Verified: `governance_db` tables queried asynchronously; `staging_db` sample ORM clients queried and validated)

### Step 5: MinIO S3 Payload Offloader [COMPLETED]
- **Objective:** Create a storage utility to offload tool execution outputs and diff summaries exceeding 5 KB to object storage.
- **Files Created:**
  - `backend/app/core/storage.py` (MinIO S3 client, automatic threshold check at 5,120 bytes, retrieval & serialization)
- **Verification:** Upload a test payload $>5\text{ KB}$, verify retrieval, and confirm payloads $<5\text{ KB}$ return inline without S3 calls. (Verified: small payload kept inline; 23.8 KB payload offloaded to `s3://agent-payloads/...` and verified via round-trip retrieval)

### Step 6: Cryptographic HITL Token Manager [COMPLETED]
- **Objective:** Implement HMAC-SHA256 signed JWT generation and validation to protect the approval gate against tampering or replay attacks.
- **Files Created:**
  - `backend/app/core/security.py` (Functions: `compute_plan_hash`, `generate_approval_token`, `verify_approval_token`, custom security exceptions)
- **Verification:** Unit test token generation, hash verification, signature tampering rejection, and expiration handling. (Verified: all 4 scenarios passed — valid signature, tampered plan block, expiration rejection, forged signature block)

---

## Phase 3: FastMCP Isolated Execution Sandbox

### Step 7: AST SQL Validator with `sqlglot` [COMPLETED]
- **Objective:** Build an Abstract Syntax Tree (AST) validation engine that programmatically blocks destructive operations.
- **Files Created:**
  - `fastmcp_server/__init__.py`
  - `fastmcp_server/ast_validator.py` (Validates AST: blocks `Drop`, `Truncate`, `Alter`; requires non-trivial `Where` on `Update`/`Delete`; converts `Delete` to soft-delete updates)
- **Verification:** Unit test against malicious payloads (e.g. `UPDATE clients SET status = 'ACTIVE'`, `DROP TABLE clients;--`, `DELETE FROM clients WHERE 1=1`). (Verified: 8 test scenarios passed — blocked missing WHERE, blocked 1=1 tautology, blocked DROP/TRUNCATE/ALTER, converted hard DELETE to soft-delete, approved valid SELECT/UPDATE)

### Step 8: Transaction & Row Pre-Image Snapshot Engine [COMPLETED]
- **Objective:** Implement safe execution that takes pre-image snapshots prior to data mutation.
- **Files Created:**
  - `fastmcp_server/tools/__init__.py`
  - `fastmcp_server/tools/transaction_executor.py` (Executes dry-runs with rollback; captures row snapshots into `governance_db.row_pre_images` before executing mutation on `staging_db`; logs to `tool_logs`)
- **Verification:** Execute an update on sample records; confirm rows are modified in `staging_db` and exact prior values are recorded in `governance_db.row_pre_images`. (Verified: dry-run simulated with zero side-effects; real mutation updated client Hans Gruber to 'DE'; `row_pre_images` stored original 'Deutschland' snapshot)

### Step 9: Reversibility & Rollback Engine [COMPLETED]
- **Objective:** Implement a deterministic rollback tool that restores modified rows directly from pre-images.
- **Files Created:**
  - `fastmcp_server/tools/rollback_executor.py` (Reads pre-images from `governance_db.row_pre_images`, builds parameterized restoration updates for `staging_db`, and seals audit trail as `ROLLED_BACK`)
- **Verification:** Trigger an update, run rollback, and assert that the row values match their pre-update state. (Verified: Hans Gruber's record mutated to 'DE' in Step 8 was rolled back; country_code accurately restored to original 'Deutschland'; audit status updated to `ROLLED_BACK`)

### Step 10: FastMCP Service Server & Container Definition [COMPLETED]
- **Objective:** Package the FastMCP tools into an isolated service.
- **Files Created:**
  - `fastmcp_server/server.py` (FastAPI microservice exposing `GET /health`, `GET /tools/schema`, `POST /tools/dry-run`, `POST /tools/execute`, `POST /tools/rollback`)
  - `fastmcp_server/requirements.txt`
  - `infra/docker/Dockerfile.fastmcp` (Optimized build with Astral `uv` for ultra-fast, pre-compiled wheel installs)
  - `.dockerignore` (Prevents sending local `.venv` and markdown docs to daemon)
  - `docker-compose.yml` (Added `fastmcp` service on port `8001` dependent on healthy PostgreSQL & Redis)
- **Verification:** Successfully containerized and deployed. Verified:
  1. `curl http://localhost:8001/health` -> `{"status":"HEALTHY","service":"fastmcp-sandbox"}`
  2. `curl http://localhost:8001/tools/schema` -> Returned real table schemas (`clients`, `invoices`) and 3-row sample previews from `staging_db`.
  3. `curl -X POST http://localhost:8001/tools/dry-run` -> Validated safe query simulation with transaction rollback.
  4. AST validation security: Destructive operations (`DROP TABLE`) and unconstrained `UPDATE` (missing `WHERE`) rejected with HTTP 400.

---

## Phase 4: Multi-Agent Reasoning Cluster (LangGraph)

### Step 11: Agent State Definition & Schema [COMPLETED]
- **Objective:** Define the centralized state schema for the LangGraph workflow.
- **Files Created:**
  - `backend/app/agents/__init__.py`
  - `backend/app/agents/state.py` (TypedDict `ComplianceState`, Pydantic models `PIIEntity`, `MigrationQuery`, `CompliancePlan`, `JudgeEvaluation`, `ExecutionResult`, and `create_initial_state` factory)
  - `backend/tests/test_agent_state.py` (Unit tests covering initialization, validation, JSON serialization, and LangGraph message reducers)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_agent_state.py -v`. 4/4 tests passed (100% pass rate). Tested full integration with AST validator, JWT HITL security, and initial state generation.

### Step 12: Guardrail Agent (PII Sanitization) [COMPLETED]
- **Objective:** Scan incoming prompts for sensitive entities (Emails, Names, IBANs, Phone Numbers) and replace them with reversible cryptographic/indexed tokens.
- **Files Created:**
  - `backend/app/agents/guardrail.py` (Integrates Microsoft Presidio Analyzer Engine configured with `en_core_web_sm` + custom European IBAN and German Steuer-ID pattern recognizers, reversible masking/unmasking, and `guardrail_node` for LangGraph)
  - `backend/tests/test_guardrail_agent.py` (5 unit tests covering standard PII masking, European financial identifiers, unmasking fidelity, same-type multiple entities, and LangGraph node state integration)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_guardrail_agent.py -v`. 5/5 tests passed. Verified total backend test suite (9/9 passed).

### Step 13: Compliance Planner Agent [COMPLETED]
- **Objective:** Introspect the target schema and generate a structured JSON data normalization plan using Groq LLMs.
- **Files Created:**
  - `backend/app/agents/planner.py` (Introspects live schema from FastMCP `/tools/schema`, queries Groq `qwen/qwen3.8-27b` in JSON Schema mode, generates valid `CompliancePlan` with mandatory WHERE clauses, preserves masked PII tokens, and supports critique reflection loops)
  - `backend/tests/test_planner_agent.py` (5 unit tests covering schema introspection, migration query generation, PII token preservation, safety judge feedback adaptation, and LangGraph node state integration)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_planner_agent.py -v`. 5/5 tests passed (100% pass rate). Total backend suite now has 14/14 tests passing.

### Step 14: Safety Judge Agent (LLM-as-a-Judge) [COMPLETED]
- **Objective:** Evaluate the proposed migration plan against security rules, scoring the plan and driving a reflection loop if rejected.
- **Files Created:**
  - `backend/app/agents/judge.py` (Hybrid auditor: deterministic pre-checks for unconstrained mutations, `WHERE 1=1`, `DROP`, hard `DELETE`; Groq LLM `qwen/qwen3.8-27b` semantic review; scores $0.0-1.0$; triggers reflection loop if score $< 0.85$, max 3 retries; routes to `PENDING_APPROVAL` when safe)
  - `backend/tests/test_judge_agent.py` (9 unit tests covering deterministic filters, tautology detection, DDL blocking, soft-delete enforcement, LLM semantic scoring, approval status transitions, and reflection retry exhaustion)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_judge_agent.py -v`. 9/9 tests passed (100% pass rate in 3.97s).

### Step 15: Execution Worker Agent [COMPLETED]
- **Objective:** Act as a deterministic relay between the approved plan and the FastMCP service.
- **Files Created:**
  - `backend/app/agents/execution_worker.py` (Cryptographic HMAC-SHA256 token verification, deterministic PII unmasking from `pii_mapping`, governance lineage recording, FastMCP `/tools/execute` dispatch with row pre-image snapshots, MinIO offloading for payloads $>5\text{ KB}$, and LangGraph state transition to `COMPLETED`)
  - `backend/tests/test_execution_worker.py` (Unit tests covering token tampering rejection, live FastMCP execution with row mutation and pre-image capture, and LangGraph node state integration)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_execution_worker.py -v`. 3/3 tests passed (100% pass rate in 12.8s).

### Step 16: Graph Assembly & AsyncPostgresSaver Checkpointing [COMPLETED]
- **Objective:** Compile the multi-agent graph with durable checkpointing and an interrupt gate at the approval node.
- **Files Created:**
  - `backend/app/agents/graph.py` (Assembles `StateGraph` with nodes `guardrail`, `planner`, `judge`, `execution_worker`; reflection loop routing back to planner if score $< 0.85$; sets `interrupt_before=["execution_worker"]` for mandatory cryptographic HITL authorization)
  - `backend/tests/test_agent_graph.py` (4 unit tests covering graph topology, conditional routing matrix, automatic pause at HITL interrupt gate, and state resumption after human cryptographic sign-off)
- **Verification:** Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_agent_graph.py -v`. 4/4 tests passed (100% pass rate). Phase 4 Multi-Agent Reasoning Cluster is fully complete.

---

## Phase 5: FastAPI Gateway, Streaming & Control API [COMPLETED]

### Step 17: Application Setup & Database Lifespan [COMPLETED]
- **Objective:** Configure the main FastAPI entrypoint, lifespan events, and Redis connection pool.
- **Files Created:**
  - `backend/app/main.py` (FastAPI app, CORS, lifespan database connection checks, router registrations)
  - `backend/app/api/schemas.py` (Validated Pydantic models with ConfigDict from_attributes)
- **Verification:** Verified `GET /health` and `GET /` returning 200 OK with active connections to dual Postgres databases, Redis, and FastMCP.

### Step 18: Pipeline Execution & SSE Streaming Endpoints [COMPLETED]
- **Objective:** Provide endpoints to submit natural language commands and stream live agent step progress via Server-Sent Events.
- **Files Created:**
  - `backend/app/core/events.py` (ThreadEventBroadcaster with Redis list replay & Pub/Sub + in-memory fallback)
  - `backend/app/api/pipeline.py` (`POST /api/pipeline/run`, `GET /api/pipeline/{thread_id}/stream`)
- **Verification:** Verified synchronous wait-for-pause and asynchronous background execution modes, and `GET /api/pipeline/{thread_id}/stream` returning `text/event-stream`.

### Step 19: Cryptographic HITL Approval & Rejection Endpoints [COMPLETED]
- **Objective:** Expose endpoints for humans to inspect pending plans, submit signed approval tokens, or reject workflows.
- **Files Created:**
  - `backend/app/api/approval.py` (`GET /api/pipeline/{thread_id}/pending`, `POST /api/pipeline/approve`, `POST /api/pipeline/reject`)
- **Verification:** Tested plan SHA-256 fingerprint generation, HMAC-SHA256 JWT approval token issuance, tampered token rejection (403 Forbidden), and graph resumption past the interrupt gate.

### Step 20: Rollback & Reversibility Endpoints [COMPLETED]
- **Objective:** Allow one-click row-level restoration of a previously executed plan.
- **Files Created:**
  - `backend/app/api/rollback.py` (`POST /api/pipeline/{execution_id}/rollback`)
- **Verification:** Tested 404 handling on unknown execution IDs and end-to-end rollback execution via FastMCP restoring pre-image snapshots.

### Step 21: Staging CRM Introspection Endpoints [COMPLETED]
- **Objective:** Expose read endpoints to allow the frontend or client to inspect dirty vs. cleaned CRM records.
- **Files Created:**
  - `backend/app/api/crm.py` (`GET /api/crm/clients`, `GET /api/crm/clients/{client_id}`, `GET /api/crm/invoices`)
- **Verification:** Verified paginated client lists, search filtering across names/emails/phones, detailed client invoice relations, and soft-delete filtering.

### Step 22: Data Retention Lifecycle & Admin Hard-Purge Service [COMPLETED]
- **Objective:** Implement the 30-day automated compliance purge for soft-deleted records and provide an authorized Admin endpoint for immediate manual permanent deletion (e.g., GDPR Right to Erasure).
- **Files Created:**
  - `backend/app/core/retention.py` (Retention purge query for records with `deleted_at < NOW() - INTERVAL '30 days'` & GDPR Right to Erasure cascade delete)
  - `backend/app/api/admin.py` (`POST /api/admin/purge`, `DELETE /api/admin/clients/{client_id}/hard-delete`, `POST /api/admin/seed-demo-data`)
- **Verification:** Tested demo seeding, 30-day retention purge confirming physical removal, and GDPR hard-delete endpoint.

**Phase 5 Verification Suite:** `backend/tests/test_api_endpoints.py` (14/14 tests passing, 100% pass rate).

---

## Phase 6: Integration Testing & Verification

### Step 23: End-to-End Test Suite [COMPLETED]
- **Objective:** Validate the entire flow from natural language prompt to PII sanitization, planning, approval interruption, execution, rollback, and data retention.
- **Files Created / Updated:**
  - `backend/tests/test_e2e_pipeline.py` (Full continuous end-to-end integration test)
  - `backend/app/api/pipeline.py` (Resilient `_safe_uuid` parser preventing crashes on non-hex LLM plan IDs)
  - `backend/app/api/approval.py` (Resilient `p_uuid` conversion)
  - `backend/app/core/config.py` & `.env` (Configured high-capacity `openai/gpt-oss-120b` and `openai/gpt-oss-20b` for Groq inference)
- **Verification:** Executed `pytest backend/tests/test_e2e_pipeline.py -v` (1/1 passed) and the complete unified integration suite `pytest backend/tests/test_api_endpoints.py backend/tests/test_e2e_pipeline.py -v` (15/15 passed, 100% pass rate).

---

## Phase 7: Next.js Frontend Implementation & End-to-End Verification [COMPLETED]

### Step 24: Backend Support & Audit Introspection Endpoints [COMPLETED]
- **Objective:** Expose execution audit logs, pre-image before/after state diffs, CRM dirty-data quality metrics, and retention horizons to the frontend.
- **Files Created / Updated:**
  - `backend/app/api/audit.py` (`GET /api/audit/executions`, `GET /api/audit/executions/{id}`, `GET /api/audit/crm-stats`, `GET /api/audit/retention-stats`)
  - `backend/app/api/__init__.py` (Registered `audit_router`)
- **Verification:** Tested `GET /api/audit/crm-stats` and `GET /api/audit/retention-stats` returning 200 OK with live database counters.

### Step 25: Next.js App Router Scaffolding & Vanilla CSS Design System [COMPLETED]
- **Objective:** Establish the frontend application using Next.js 16 with TypeScript and a custom dark enterprise Vanilla CSS design system.
- **Files Created:**
  - `frontend/package.json` (Next.js 16, React 19, TypeScript)
  - `frontend/src/app/globals.css` (Bespoke design system: glassmorphism, responsive CSS grid, color palette `#0B0F19`, `#1E293B`, `#38BDF8`, `#22C55E`, `#F59E0B`, micro-animations)
  - `frontend/src/app/layout.tsx` (Root layout with Inter & Outfit typography and SEO metadata)
  - `frontend/src/lib/types.ts` & `frontend/src/lib/api.ts` (Strongly-typed API contracts matching backend schemas)
  - `frontend/src/hooks/usePipeline.ts` (Reactive Server-Sent Events hook streaming graph stages, PII counters, and HITL state)

### Step 26: Reusable Interactive Components [COMPLETED]
- **Objective:** Build accessible, native `<dialog>` modals, pipeline visualizations, and syntax highlighters.
- **Files Created:**
  - `frontend/src/components/Modal.tsx` (Accessible `<dialog>` with `closedby="any"`, focus containment, and light-dismiss fallback)
  - `frontend/src/components/AppShell.tsx` (Enterprise sidebar, live health indicator polling `/health`, demo data seeder)
  - `frontend/src/components/AgentPipelineFlow.tsx` (5-stage multi-agent animated flow bar)
  - `frontend/src/components/ScoreGauge.tsx` (SVG circular score gauge for LLM Judge ratings)
  - `frontend/src/components/SqlViewer.tsx` (Lightweight zero-dependency SQL keyword/string syntax highlighter)
  - `frontend/src/components/HitlApprovalModal.tsx` (Human approval gate review modal with SHA-256 plan hash, critique, and digital signature actions)
  - `frontend/src/components/PreImageDiffModal.tsx` (Before-and-after state diff inspector with red/green row field changes)
  - `frontend/src/components/StatusBadge.tsx` & `frontend/src/components/Toast.tsx`

### Step 27: Enterprise Pages & End-to-End Verification [COMPLETED]
- **Objective:** Implement all four operator pages and verify end-to-end with the backend.
- **Files Created:**
  - `frontend/src/app/page.tsx` (Agent Cockpit with preset compliance directive chips, SSE live log, and approval gate)
  - `frontend/src/app/crm/page.tsx` (CRM Explorer with clients & invoices tabs, dirty-data quality badges, search, and client detail drawer)
  - `frontend/src/app/audit/page.tsx` (Audit & Rollback Explorer with execution history and 1-click rollback)
  - `frontend/src/app/governance/page.tsx` (GDPR Article 17 "Right to be Forgotten" hard-delete console and 30-day retention purge)
- **Verification:**
  - Executed `npm run build` in `frontend/` (Static generation 4/4 pages passed with zero TypeScript or lint errors).
  - Executed dev server and confirmed all 4 routes (`/`, `/crm`, `/audit`, `/governance`) respond with HTTP 200 OK.
  - Full end-to-end workflow confirmed in browser subagent.

---

## Phase 8: Admin Authentication & Session Management [COMPLETED]

### Step 28: Backend Cryptographic Admin Auth Layer [COMPLETED]
- **Objective:** Provide secure administrative authentication using salted PBKDF2-HMAC-SHA256 password digests and cryptographically signed JWT bearer tokens.
- **Files Created / Updated:**
  - `backend/app/core/config.py` (Added `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_EMAIL`, `AUTH_TOKEN_EXPIRE_MINUTES`)
  - `backend/app/core/security.py` (Added `create_access_token`, `verify_access_token`, `hash_password`, `verify_admin_password`)
  - `backend/app/api/schemas.py` (Added `LoginRequest`, `UserOut`, `LoginResponse`)
  - `backend/app/api/auth.py` (Implemented `POST /api/auth/login`, `GET /api/auth/me`, and `get_current_admin` security dependency)
  - `backend/app/api/__init__.py` (Registered `auth_router`)
  - `backend/tests/test_auth.py` (4/4 unit tests passing: valid login, email login, wrong password rejection, protected `/me` token verification)
- **Verification:** Executed `pytest backend/tests/test_auth.py -v` (4/4 passed in 1.70s). Combined API test suite `pytest backend/tests/test_auth.py backend/tests/test_api_endpoints.py -v` (18/18 passed, 100% pass rate).

### Step 29: Frontend Admin Context & Strict Route Lockdown [COMPLETED]
- **Objective:** Build an administrative authentication workflow in the Next.js frontend with persistent session management, automatic bearer token injection, user profile state, and strict route lockdown.
- **Files Created / Updated:**
  - `frontend/src/lib/types.ts` (Added `UserOut` and `LoginResponse` interfaces)
  - `frontend/src/lib/api.ts` (Added `getAuthToken`, `setAuthToken`, `clearAuthToken`, automatic `Authorization: Bearer <token>` injection, `api.login`, `api.me`)
  - `frontend/src/context/AuthContext.tsx` (React Context providing `user`, `login`, `logout`, `isAuthenticated`, `loading`)
  - `frontend/src/app/login/page.tsx` (Dedicated admin login screen with dark glassmorphism card, demo credentials quick-fill, and error handling)
  - `frontend/src/components/AppShell.tsx` (Enforces strict full-application lockdown: unauthenticated requests to `/`, `/crm`, `/audit`, `/governance` are automatically blocked and redirected to `/login`; displays session verification spinner, verified user badge in Topbar, and Sign Out action)
- **Verification:**
  - Executed `npm run build` in `frontend/` (Compiled 8/8 routes including `/login` with 0 TypeScript/lint errors).
  - Verified `curl http://localhost:3000/login` returning HTTP 200 OK.
  - Verified end-to-end token flow with `POST /api/auth/login` and `GET /api/auth/me`.
  - Verified unauthenticated visitors are barred from application pages and redirected to `/login`.

---

## Phase 9: Informational & Read-Only Query Results Pipeline [COMPLETED]

### Step 30: FastMCP Sandbox Read Execution & Dedicated Result UI [COMPLETED]
- **Objective:** Support read-only client queries (e.g., *"can you give me info of this client erika.mustermann@web.de"*), executing them securely via FastMCP `/tools/dry-run`, capturing output records into `agent_output["query_results"]`, streaming them via the audit/approval APIs, and rendering them in the frontend Cockpit and Execution Audit modal.
- **Files Created / Updated:**
  - `backend/app/agents/state.py` (Added `query_results: list[dict[str, Any]]` to `ExecutionResult`)
  - `backend/app/api/schemas.py` (Added `query_results` field to `ApprovePlanResponse`)
  - `backend/app/agents/execution_worker.py` (Dispatched `SELECT` statements to FastMCP `/tools/dry-run`, collected `preview_sample`, unmasked PII, and persisted rows into `AgentExecution.agent_output["query_results"]`)
  - `backend/app/api/approval.py` (Broadcasted and returned `query_results` in `ApprovePlanResponse` and websocket/SSE events)
  - `backend/app/api/audit.py` (Extracted `query_results` and `is_read_only` in `get_execution_detail`, with dynamic on-the-fly resolution for historical executions)
  - `frontend/src/lib/types.ts` (Added `query_results` and `is_read_only` to `ExecutionDetail` and `ApproveResponse`)
  - `frontend/src/components/PreImageDiffModal.tsx` (Rendered dedicated "📋 Retrieved Records / Query Output" tabular card and read-only informational banner)
  - `frontend/src/app/page.tsx` (Rendered retrieved live CRM entity table within the Cockpit completion card)
- **Verification:**
  - Executed `curl http://localhost:8000/api/audit/executions/7b241165-191c-492b-9ef6-86752b0d85fa` and confirmed Erika Mustermann's full record (name, email, phone, address, city, GDPR consent status) was returned with `is_read_only = true`.
  - Executed `PYTHONPATH=. .venv/bin/pytest backend/tests/test_execution_worker.py backend/tests/test_api_endpoints.py backend/tests/test_auth.py` (21/21 passed, 100% pass rate).
  - Executed `npm run build` in `frontend/` (0 errors, 8/8 routes compiled).

---

## Overall Implementation Status: 100% COMPLETE (Backend + Frontend + Auth + Read-Only Pipeline)
All 30 steps across Infrastructure, Database Layer, Sandbox Engine, Multi-Agent Reasoning Graph, FastAPI Gateway, E2E Integration Testing, Next.js Enterprise Cockpit, Admin Authentication, and Informational Query Pipeline are fully implemented, verified, and operational.


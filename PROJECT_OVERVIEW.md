# Autonomous Data Cleaning & Governance Multi-Agent Platform

> **Project Brief for AI Agents, LLM Interviewers, and Technical Reviewers**  
> *Use this document as full system context to ask architectural, technical, debugging, and interview questions about this project.*

---

## 1. Executive Summary & Problem Statement

### The Problem
Traditional data cleaning pipelines in modern enterprises rely either on static, brittle regex/SQL transformation scripts or risky, manual analyst intervention. When companies attempt to use LLMs for automated database maintenance, they face catastrophic risks:
1. **Unbounded Hallucinations:** An LLM might generate `UPDATE customers SET email = NULL` without a `WHERE` clause, or hallucinate destructive DDL (`DROP TABLE`, `TRUNCATE`).
2. **Security & Prompt Injection:** Malicious payloads inside user records or CSVs can hijack the agent to execute arbitrary SQL or exfiltrate private database credentials.
3. **Lack of Deterministic Recovery:** Once an LLM corrupts thousands of database rows across multiple tables, standard database backups require full database rollbacks, causing production downtime and losing valid concurrent transactions.
4. **Missing Human Governance:** Fully autonomous execution without oversight violates enterprise data governance standards (SOC2, HIPAA, GDPR).

### The Solution
The **Autonomous Data Cleaning & Governance Agent** is an enterprise-grade multi-agent platform designed with a **Zero-Trust Security & Execution Sandbox**:
* **Cyclical Multi-Agent Orchestration:** Powered by **LangGraph**, decomposing data cleaning into 4 discrete, specialized agents (Planner, Guardrail, Execution Worker, and LLM Judge).
* **Air-Gapped FastMCP Execution Sandbox:** Decouples the AI agents from database credentials. Agents never have direct database access; queries are dispatched via JSON-RPC to an isolated FastMCP microservice.
* **AST Security Guardrails:** Every query passes through an Abstract Syntax Tree (AST) parser (`sqlglot`) to block destructive DDL and enforce parameterized `WHERE` boundaries.
* **Deterministic Pre-Image Rollback Engine:** Automatically captures row-level JSON pre-images before mutation, guaranteeing one-click, point-in-time atomic rollbacks.
* **Human-in-the-Loop (HITL) Checkpoints:** Asynchronously pauses execution after planning and dry-run simulations, resuming only when an authorized human approves.
* **Distributed Observability:** Complete multi-agent tracing and latency profiling instrumented via **LangSmith**.

---

## 2. Technology Stack & Cloud Infrastructure

| Category | Technology | Purpose in This Project |
| :--- | :--- | :--- |
| **Agent Orchestrator** | **LangGraph / LangChain** | StateGraph engine managing cyclical multi-agent workflows, state persistence, and HITL pauses. |
| **LLM Inference** | **Groq Cloud** (`qwen-2.5-coder-32b`, `llama-3.3-70b-versatile`) | Ultra-low latency structured reasoning for planning, guardrailing, and evaluation. |
| **Sandbox & Tools** | **FastMCP (Model Context Protocol)** | Independent Python microservice exposing AST-validated database tools over JSON-RPC/SSE. |
| **SQL AST Engine** | **sqlglot** | Deep syntax tree inspection to block multi-statement injection, DDL commands, and unconstrained updates. |
| **Databases** | **Neon PostgreSQL (Serverless)** | Dual-database architecture: **Governance DB** (audit & state) and **Staging DB** (isolated CRM records). |
| **Backend API** | **FastAPI (Python 3.11+, AsyncIO, SQLAlchemy)** | Asynchronous REST API, Server-Sent Events (SSE) streaming, and approval webhooks. |
| **Observability** | **LangSmith** | Distributed tracing, prompt versioning, token cost tracking, and agent telemetry. |
| **Object Storage** | **Cloudflare R2 / MinIO** | S3-compatible offload for oversized execution logs, pre-image blobs, and audit payloads (>5KB). |
| **Frontend UI** | **Next.js 14 (React, TypeScript, TailwindCSS)** | Modern dashboard featuring pipeline control, diff inspection, schema preview, and audit trail. |
| **Cloud Hosting** | **Render (Backend & FastMCP) + Vercel (Frontend)** | Production deployment with automated CI/CD and containerized services. |

---

## 3. High-Level Architecture Diagram

```mermaid
flowchart TD
    User([User / Data Analyst]) -->|1. Natural Language Goal| NextJS[Frontend: Next.js 14 Dashboard]
    NextJS -->|2. REST / SSE Stream| FastAPI[Backend: FastAPI Orchestration API]
    
    subgraph MultiAgent["LangGraph StateGraph Engine"]
        Planner["1. Planner Agent (Groq)<br/>Introspects schema & proposes steps"]
        Guardrail["2. Guardrail Agent (sqlglot)<br/>Dry-run simulation & blast radius check"]
        HITL{3. Human-in-the-Loop Check<br/>Requires Human Approval?}
        Worker["4. Execution Worker Agent<br/>Dispatches steps to FastMCP sandbox"]
        Judge["5. LLM Judge Agent (Groq)<br/>Validates fidelity against Master Goal"]
        
        Planner --> Guardrail
        Guardrail --> HITL
        HITL -->|Requires Approval| Paused[Paused: Awaiting Admin Approval]
        HITL -->|Auto-Approved or Human Click| Worker
        Worker --> Judge
    end

    FastAPI --> MultiAgent
    Paused -.->|Resume via /api/approval/{id}| Worker

    subgraph Sandbox["FastMCP Tool Microservice (Port 8001)"]
        AST["AST Validator (sqlglot)<br/>Blocks DROP, TRUNCATE, ALTER"]
        PreImg["Pre-Image Engine<br/>SELECT * WHERE ... before mutation"]
        Exec["Transactional Executor<br/>Runs mutation & commits"]
        AST --> PreImg --> Exec
    end

    Worker -->|JSON-RPC Tool Calls| Sandbox
    Exec -->|Atomic Writes| StagingDB[(Neon Staging DB: CRM Tables)]
    PreImg -->|JSON Snapshots| GovDB[(Neon Governance DB: Audits & Plans)]
    Judge -->|Score & Summary| FastAPI
    MultiAgent -.->|Telemetry & Tracing| LangSmith[(LangSmith Telemetry Cloud)]
```

---

## 4. The 4 Specialized Agents in the Graph

### 1. The Planner Agent (`backend/app/agents/planner.py`)
* **Role:** Translates high-level natural language goals into a structured, step-by-step cleaning plan.
* **Mechanism:** Queries `GET /tools/schema` on FastMCP to introspect actual column names, data types, and 3-row data samples. Generates a unique plan (`uuid.uuid4()`) containing atomic SQL queries.
* **Output:** A strongly typed `ExecutionPlan` with discrete `PlanStep` objects.

### 2. The Guardrail Agent (`backend/app/agents/guardrail.py`)
* **Role:** Evaluates the proposed plan before execution to determine safety and risk level.
* **Mechanism:** 
  * Calls `POST /tools/dry-run` on FastMCP to simulate the query in an auto-rolled-back transaction.
  * Measures affected row counts against enterprise thresholds.
  * Assigns a risk level: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`.
  * If risk is high or mutation exceeds thresholds, forces a Human-in-the-Loop checkpoint (`AWAITING_APPROVAL`).

### 3. The Execution Worker Agent (`backend/app/agents/execution_worker.py`)
* **Role:** Executes approved steps sequentially against the database.
* **Mechanism:** The worker **never** executes SQL queries directly. It packages the `execution_id` and query string into an envelope and calls FastMCP's `POST /tools/execute`. It tracks execution metrics, step statuses, and handles tool errors.

### 4. The LLM Judge Agent (`backend/app/agents/judge.py`)
* **Role:** Independent evaluation of pipeline fidelity.
* **Mechanism:** Reads the initial `master_goal`, the executed SQL statements, and the resulting database state. Outputs a quantitative confidence score from `0.0` to `1.0` and qualitative feedback.
* **Threshold:** If the judge score is `< 0.85`, the execution is flagged as degraded or requiring manual audit.

---

## 5. Security & Sandboxing Architecture

### Why FastMCP?
The architecture enforces a strict **separation between "The Brain" (LLM) and "The Muscle" (Database Access)**:
1. **Zero Direct Credentials:** The agent processes and LangGraph orchestrator do not hold database connection strings or write credentials for the data tables.
2. **Protocol Decoupling:** Tool calls follow the Model Context Protocol (MCP) standard, enabling any MCP-compatible agent or client to interact with the sandbox securely.
3. **AST Policy Enforcement (`ast_validator.py`):**
   * Uses `sqlglot` to parse incoming queries into syntax trees.
   * Disallows DDL (`DROP`, `TRUNCATE`, `ALTER`, `CREATE`).
   * Blocks multi-statement query chaining (preventing `; DROP TABLE ...` injections).
   * Enforces that all `UPDATE` and `DELETE` queries contain explicit, non-empty `WHERE` clauses.

### Dual-Database Isolation Pattern
* **Governance Database (`GOVERNANCE_DATABASE_URL`):** Stores agent states, execution runs, approval tokens, immutable audit logs, and row pre-images. Agents have no permission to mutate data here.
* **Staging Database (`STAGING_DATABASE_URL`):** Contains the actual application data (e.g., `crm_leads`, `contacts`, `customers`). Completely isolated from production databases.

---

## 6. Deterministic Point-in-Time Rollback Engine

The rollback engine provides mathematical certainty that any corrupted state can be completely restored:

```
[Agent Submits Mutation: UPDATE crm_leads SET phone = '...' WHERE id IN (1, 2)]
                                  │
                                  ▼
[1. AST Extraction: Table='crm_leads', Where='id IN (1, 2)']
                                  │
                                  ▼
[2. Capture Pre-Images: SELECT * FROM crm_leads WHERE id IN (1, 2)]
                                  │
                                  ▼
[3. Persist Snapshots into governance_db.row_pre_images]
  - execution_id: uuid
  - table_name: "crm_leads"
  - row_pk: "1"
  - data_before: '{"id": 1, "phone": "(555) 019-2834", "email": "ALICE@RAW.COM"}'
                                  │
                                  ▼
[4. Execute Mutation on staging_db & COMMIT]
```

### Rollback Execution (`rollback_executor.py`)
When a rollback is triggered via `POST /tools/rollback`:
1. Queries `row_pre_images` matching the target `execution_id`.
2. Synthesizes parameterized `UPDATE` queries matching on the primary key, resetting every modified column to its exact original JSON value.
3. Commits the rollback inside a single atomic transaction.
4. Updates the execution audit record to `ROLLED_BACK`.

---

## 7. FastMCP Server Endpoints (`fastmcp_server/server.py`)

| Endpoint | Method | Input | Behavior |
| :--- | :--- | :--- | :--- |
| `/tools/schema` | `GET` | `table_name?` | Introspects `information_schema.columns` and returns column definitions and 3-row sample data. |
| `/tools/dry-run` | `POST` | `sql_query` | Simulates query in an uncommitted transaction; returns predicted affected rows. |
| `/tools/execute` | `POST` | `execution_id, sql_query` | Validates AST, snapshots pre-images to Gov DB, executes mutation on Staging DB, and commits. |
| `/tools/rollback`| `POST` | `execution_id` | Restores original row states from pre-images in an atomic transaction. |
| `/health` | `GET` | None | Service liveness probe. |

---

## 8. Backend API Endpoints (`backend/app/api/`)

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/pipeline/run` | `POST` | Triggers a new data cleaning pipeline run from a natural language goal. |
| `/api/pipeline/stream/{execution_id}` | `GET` | Streams real-time agent execution events over Server-Sent Events (SSE). |
| `/api/approval/{plan_id}` | `POST` | Approves or rejects a paused plan, resuming the LangGraph workflow. |
| `/api/rollback/{execution_id}` | `POST` | Triggers point-in-time restoration of all affected rows. |
| `/api/audit/executions` | `GET` | Fetches historical runs ordered chronologically (newest first). |
| `/api/crm/preview/{table_name}` | `GET` | Fetches current rows from the Neon Staging database for live UI preview. |

---

## 9. Key Database Models (`backend/app/db/models.py`)

* **`AgentExecution`:** Tracks lifecycle state (`PLANNED`, `AWAITING_APPROVAL`, `RUNNING`, `COMPLETED`, `FAILED`, `ROLLED_BACK`), timing, target table, and judge score.
* **`ExecutionPlan`:** Stores the structured plan, risk level, dry-run row counts, and JSON step list.
* **`RowPreImage`:** Immutable snapshot of row states prior to mutation (`execution_id`, `table_name`, `row_pk`, `data_before`).
* **`AuditLog`:** Detailed event trail capturing every agent node transition and error.
* **`ToolLog`:** Records tool inputs, outputs, execution latencies, and optional S3/R2 storage references.

---

## 10. Frequently Asked Questions & Interview Deep-Dives

### Q1: Why LangGraph instead of standard linear LangChain or CrewAI?
> **Answer:** Standard LLM chains are directed acyclic graphs (DAGs) designed for single-pass prompt sequences. Data cleaning requires **cycles** (e.g., replanning when a dry-run fails) and **asynchronous checkpointing**. With LangGraph, when the Guardrail flags a high-risk mutation, the graph enters a checkpointed state and pauses execution. Hours later, when an admin clicks "Approve" on the frontend, LangGraph rehydrates state from PostgreSQL and resumes execution seamlessly.

### Q2: How do you prevent an LLM from hallucinating destructive SQL?
> **Answer:** Defense-in-depth:
> 1. The LLM has zero direct database credentials.
> 2. All queries pass to our FastMCP microservice.
> 3. FastMCP's AST validator (`sqlglot`) parses the query tree before opening a DB connection, blocking all DDL (`DROP`, `TRUNCATE`, `ALTER`) and rejecting any `UPDATE` or `DELETE` that lacks a qualified `WHERE` clause.
> 4. All mutations run against an isolated Neon Staging replica, keeping production completely air-gapped.

### Q3: What makes your rollback mechanism superior to database snapshots?
> **Answer:** Traditional database snapshots (e.g., AWS RDS snapshots or Neon branch restores) are coarse-grained: rolling back a snapshot wipes out **all** transactions that occurred across the entire database since the snapshot was taken. Our pre-image engine works at the **row level**: it only rolls back the specific rows modified by that specific `execution_id`, leaving all concurrent transactions untouched.

### Q4: How is observability handled?
> **Answer:** We integrated **LangSmith** distributed tracing across all agent nodes. Every LLM generation, token count, latency metric, tool call, and state transition is captured. When inspecting executions, developers can view the exact prompt sent to Groq, the AST validation logs, and the LLM Judge's scoring rationale. Large tool outputs (>5KB) are automatically offloaded to Cloudflare R2 / MinIO to prevent database bloat.

-- ==============================================================================
-- 02_governance_schema.sql
-- Sets up the governance, audit, checkpointing, and pre-image tables in governance_db
-- ==============================================================================

\connect governance_db;

-- Enable UUID extension if not already available
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enum Types for Workflow & Agent Lifecycle
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workflow_status') THEN
        CREATE TYPE workflow_status AS ENUM (
            'PENDING_REASONING',
            'PENDING_APPROVAL',
            'APPROVED',
            'REJECTED',
            'EXECUTING',
            'COMPLETED',
            'ROLLED_BACK',
            'FAILED'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'agent_role') THEN
        CREATE TYPE agent_role AS ENUM (
            'SUPERVISOR',
            'GUARDRAIL',
            'COMPLIANCE_PLANNER',
            'SAFETY_JUDGE',
            'EXECUTION_WORKER'
        );
    END IF;
END$$;

-- 1. Supervisor Threads (Session Tracking)
CREATE TABLE IF NOT EXISTS supervisor_threads (
    thread_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    master_goal TEXT NOT NULL,
    current_status workflow_status DEFAULT 'PENDING_REASONING',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Message History (Prompts & Responses with PII Sanitization Flag)
CREATE TABLE IF NOT EXISTS message_history (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id UUID NOT NULL REFERENCES supervisor_threads(thread_id) ON DELETE CASCADE,
    role agent_role NOT NULL,
    content TEXT NOT NULL,
    sanitized_pii BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Execution Plans (Proposed SQL, Judge Score & Cryptographic Token Hash)
CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id UUID NOT NULL REFERENCES supervisor_threads(thread_id) ON DELETE CASCADE,
    proposed_sql_plan JSONB NOT NULL,
    judge_score NUMERIC(3,2),
    judge_feedback TEXT,
    approval_token_hash VARCHAR(512),
    status workflow_status DEFAULT 'PENDING_APPROVAL',
    approved_by VARCHAR(255),
    approved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. Agent Executions Trace
CREATE TABLE IF NOT EXISTS agent_executions (
    execution_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES execution_plans(plan_id) ON DELETE CASCADE,
    agent_name agent_role NOT NULL,
    sub_task_input JSONB NOT NULL,
    agent_output JSONB,
    retry_count INT DEFAULT 0,
    status workflow_status DEFAULT 'EXECUTING',
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

-- 5. Tool Logs (Tool invocation audit & MinIO payload pointer)
CREATE TABLE IF NOT EXISTS tool_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID NOT NULL REFERENCES agent_executions(execution_id) ON DELETE CASCADE,
    tool_name VARCHAR(255) NOT NULL,
    input_parameters JSONB NOT NULL,
    output_summary TEXT,
    payload_s3_uri VARCHAR(1024), -- Set if tool response payload > 5KB
    is_dry_run BOOLEAN DEFAULT TRUE,
    execution_status VARCHAR(50) NOT NULL,
    executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 6. Immutable Row Pre-Images (Safety Net 2: Instant Rollbacks)
CREATE TABLE IF NOT EXISTS row_pre_images (
    pre_image_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID NOT NULL REFERENCES agent_executions(execution_id) ON DELETE CASCADE,
    target_table VARCHAR(255) NOT NULL,
    primary_key_value VARCHAR(255) NOT NULL,
    pre_image_state JSONB NOT NULL, -- Exact row snapshot before update
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for Fast Audit Trail Lookups
CREATE INDEX IF NOT EXISTS idx_threads_user ON supervisor_threads(user_id);
CREATE INDEX IF NOT EXISTS idx_plans_thread ON execution_plans(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_exec_plan ON agent_executions(plan_id);
CREATE INDEX IF NOT EXISTS idx_tool_logs_execution ON tool_logs(execution_id);
CREATE INDEX IF NOT EXISTS idx_pre_images_exec ON row_pre_images(execution_id);
CREATE INDEX IF NOT EXISTS idx_pre_images_table_pk ON row_pre_images(target_table, primary_key_value);

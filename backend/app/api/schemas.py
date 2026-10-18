"""
Pydantic Request and Response Schemas for the FastAPI Gateway.

Provides structured, validated, and self-documenting data transfer objects
for pipeline execution, HITL approval, rollback, CRM introspection, and administration.
"""

from datetime import date, datetime
from typing import Any, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field


# ==============================================================================
# Pipeline Run & Streaming Schemas
# ==============================================================================


class PipelineRunRequest(BaseModel):
    user_prompt: str = Field(
        ...,
        min_length=3,
        description="Natural language compliance, migration, or normalization instruction",
        examples=["Standardize phone numbers and anonymize emails for clients in Germany"],
    )
    user_id: str = Field(
        default="compliance_officer_01",
        description="ID of the authenticated compliance user initiating the request",
    )
    wait_for_pause: bool = Field(
        default=True,
        description="If True, execution waits until the HITL interrupt gate before returning",
    )


class PipelineRunResponse(BaseModel):
    thread_id: str = Field(..., description="Unique UUID tracking the LangGraph workflow thread")
    status: str = Field(..., description="Current status of the pipeline (e.g. PENDING_APPROVAL, EXECUTING)")
    message: str = Field(..., description="Descriptive status message")
    detected_pii_count: int = Field(0, description="Count of PII entities identified and masked")


# ==============================================================================
# Human-in-the-Loop (HITL) Approval Schemas
# ==============================================================================


class PendingApprovalResponse(BaseModel):
    thread_id: str = Field(..., description="Thread ID paused at the HITL gate")
    plan_id: str = Field(..., description="Plan ID proposed by the Compliance Planner Agent")
    plan_hash: str = Field(..., description="Deterministic SHA-256 fingerprint of the proposed SQL migration")
    approval_token: str = Field(..., description="Cryptographically signed HMAC-SHA256 JWT approval token")
    status: str = Field(..., description="Current workflow status (PENDING_APPROVAL)")
    proposed_plan: dict[str, Any] = Field(..., description="The structured plan containing SQL queries and rationales")
    judge_evaluation: Optional[dict[str, Any]] = Field(None, description="Safety Judge score, verdict, and critique")
    detected_pii: list[dict[str, Any]] = Field(default_factory=list, description="Sanitized PII entity mappings")


class ApprovePlanRequest(BaseModel):
    thread_id: str = Field(..., description="Target workflow thread ID")
    approval_token: str = Field(..., description="HMAC-SHA256 JWT approval token signed for this plan")
    approver_id: str = Field(default="compliance_officer_01", description="Identifier of the approving officer")


class ApprovePlanResponse(BaseModel):
    thread_id: str = Field(..., description="Target workflow thread ID")
    execution_id: Optional[str] = Field(None, description="UUID of the completed AgentExecution audit record")
    status: str = Field(..., description="Execution status (COMPLETED or FAILED)")
    total_affected_rows: int = Field(0, description="Total rows modified across all SQL queries")
    pre_images_captured: int = Field(0, description="Count of pre-image row snapshots preserved for rollback")
    payload_s3_uri: Optional[str] = Field(None, description="MinIO S3 URI if payload exceeded offload threshold")
    message: str = Field(..., description="Execution outcome summary")
    query_results: list[dict[str, Any]] = Field(default_factory=list, description="Retrieved rows for read-only queries")


class RejectPlanRequest(BaseModel):
    thread_id: str = Field(..., description="Target workflow thread ID")
    rejection_reason: str = Field(..., min_length=3, description="Compliance justification for rejecting the plan")
    rejected_by: str = Field(default="compliance_officer_01", description="Identifier of the rejecting officer")


class RejectPlanResponse(BaseModel):
    thread_id: str = Field(..., description="Target workflow thread ID")
    status: str = Field(default="REJECTED", description="Terminal status")
    rejection_reason: str = Field(..., description="Recorded rejection rationale")
    message: str = Field(..., description="Rejection confirmation message")


# ==============================================================================
# Rollback Schemas
# ==============================================================================


class RollbackRequest(BaseModel):
    reason: Optional[str] = Field(
        default="Manual compliance officer rollback",
        description="Reason for rolling back the executed transaction",
    )


class RollbackResponse(BaseModel):
    execution_id: str = Field(..., description="Execution ID that was reverted")
    status: str = Field(..., description="Rollback outcome (ROLLED_BACK)")
    restored_rows: int = Field(0, description="Number of rows restored to their original pre-image values")
    target_tables: list[str] = Field(default_factory=list, description="Tables affected by the rollback")
    message: str = Field(..., description="Outcome summary")


# ==============================================================================
# Staging CRM Introspection Schemas
# ==============================================================================


class ClientOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    billing_address: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    country_code: Optional[str] = None
    gdpr_consent: bool
    consent_date: Optional[date] = None
    status: str
    is_deleted: bool
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvoiceOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    invoice_number: str
    amount_raw: Optional[str] = None
    amount_standardized: Optional[float] = None
    currency: Optional[str] = None
    invoice_date: date
    is_deleted: bool
    deleted_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClientDetailOut(ClientOut):
    invoices: list[InvoiceOut] = Field(default_factory=list)


# ==============================================================================
# Admin & Retention Schemas
# ==============================================================================


class PurgeRequest(BaseModel):
    retention_days: int = Field(
        default=30,
        ge=1,
        description="Threshold days for soft-deleted records to be permanently purged",
    )


class PurgeResponse(BaseModel):
    status: str
    retention_days: int
    cutoff_timestamp: str
    purged_clients: int
    purged_invoices: int
    total_purged_rows: int


# ==============================================================================
# Health & Status Schemas
# ==============================================================================


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    postgres_governance_db: str
    postgres_staging_db: str
    redis: str
    fastmcp_sandbox: str


# ==============================================================================
# Admin Authentication Schemas
# ==============================================================================


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, description="Admin username or email")
    password: str = Field(..., min_length=4, description="Admin password")


class UserOut(BaseModel):
    username: str
    email: str
    role: str
    is_admin: bool = True


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut

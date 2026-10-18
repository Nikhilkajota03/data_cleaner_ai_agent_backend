import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ==============================================================================
# Enumerations Matching PostgreSQL Database Types
# ==============================================================================


class WorkflowStatus(str, Enum):
    PENDING_REASONING = "PENDING_REASONING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class AgentRole(str, Enum):
    SUPERVISOR = "SUPERVISOR"
    GUARDRAIL = "GUARDRAIL"
    COMPLIANCE_PLANNER = "COMPLIANCE_PLANNER"
    SAFETY_JUDGE = "SAFETY_JUDGE"
    EXECUTION_WORKER = "EXECUTION_WORKER"


# ==============================================================================
# Base Declarative Classes
# ==============================================================================


class Base(DeclarativeBase):
    """Base class for governance_db models."""

    pass


class StagingBase(DeclarativeBase):
    """Base class for staging_db models."""

    pass


# ==============================================================================
# Governance Database Models (governance_db)
# ==============================================================================


class SupervisorThread(Base):
    __tablename__ = "supervisor_threads"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    master_goal: Mapped[str] = mapped_column(Text, nullable=False)
    current_status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(WorkflowStatus, name="workflow_status", create_type=False),
        default=WorkflowStatus.PENDING_REASONING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    messages: Mapped[list["MessageHistory"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )
    plans: Mapped[list["ExecutionPlan"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class MessageHistory(Base):
    __tablename__ = "message_history"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supervisor_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[AgentRole] = mapped_column(
        SAEnum(AgentRole, name="agent_role", create_type=False), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sanitized_pii: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    thread: Mapped["SupervisorThread"] = relationship(back_populates="messages")


class ExecutionPlan(Base):
    __tablename__ = "execution_plans"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supervisor_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_sql_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    judge_score: Mapped[Optional[float]] = mapped_column(
        Numeric(3, 2), nullable=True
    )
    judge_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approval_token_hash: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(WorkflowStatus, name="workflow_status", create_type=False),
        default=WorkflowStatus.PENDING_APPROVAL,
        nullable=False,
    )
    approved_by: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    thread: Mapped["SupervisorThread"] = relationship(back_populates="plans")
    executions: Mapped[list["AgentExecution"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class AgentExecution(Base):
    __tablename__ = "agent_executions"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_plans.plan_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_name: Mapped[AgentRole] = mapped_column(
        SAEnum(AgentRole, name="agent_role", create_type=False), nullable=False
    )
    sub_task_input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_output: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(WorkflowStatus, name="workflow_status", create_type=False),
        default=WorkflowStatus.EXECUTING,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    plan: Mapped["ExecutionPlan"] = relationship(back_populates="executions")
    tool_logs: Mapped[list["ToolLog"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    pre_images: Mapped[list["RowPreImage"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )


class ToolLog(Base):
    __tablename__ = "tool_logs"

    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_executions.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    output_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload_s3_uri: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    is_dry_run: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    execution_status: Mapped[str] = mapped_column(String(50), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    execution: Mapped["AgentExecution"] = relationship(back_populates="tool_logs")


class RowPreImage(Base):
    __tablename__ = "row_pre_images"

    pre_image_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_executions.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_table: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_key_value: Mapped[str] = mapped_column(String(255), nullable=False)
    pre_image_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    execution: Mapped["AgentExecution"] = relationship(back_populates="pre_images")


# ==============================================================================
# Staging Database Models (staging_db)
# ==============================================================================


class Client(StagingBase):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    billing_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country_code: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )
    gdpr_consent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    consent_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), default="PENDING", nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Invoice(StagingBase):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_raw: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    amount_standardized: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    client: Mapped["Client"] = relationship(back_populates="invoices")

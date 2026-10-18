import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.app.db.session import AsyncStagingSessionLocal, close_db_connections
from fastmcp_server.ast_validator import ASTValidationError
from fastmcp_server.tools.rollback_executor import rollback_executor
from fastmcp_server.tools.transaction_executor import transaction_executor

# Initialize FastAPI Microservice for FastMCP Sandbox
app = FastAPI(
    title="FastMCP Database Execution Sandbox",
    description="Air-gapped MCP execution service enforcing AST security, pre-images, and rollbacks.",
    version="1.0.0",
)


# ==============================================================================
# Pydantic Schemas for Tool Invocation
# ==============================================================================


class DryRunRequest(BaseModel):
    sql_query: str = Field(..., description="The SQL query to simulate.")


class ExecuteMutationRequest(BaseModel):
    execution_id: uuid.UUID = Field(..., description="Unique agent execution ID.")
    sql_query: str = Field(..., description="The approved SQL mutation to execute.")


class RollbackRequest(BaseModel):
    execution_id: uuid.UUID = Field(
        ..., description="Execution ID whose pre-images should be restored."
    )


# ==============================================================================
# Lifecycle & Health Endpoints
# ==============================================================================


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await close_db_connections()


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {"status": "HEALTHY", "service": "fastmcp-sandbox"}


# ==============================================================================
# FastMCP Tool Endpoints
# ==============================================================================


@app.get("/tools/schema", tags=["Tools"])
async def inspect_schema(
    table_name: Optional[str] = Query(
        None, description="Optional target table name to filter."
    )
) -> dict[str, Any]:
    """
    Introspects the target staging database schema and returns column definitions,
    data types, and a 3-row data sample.
    """
    async with AsyncStagingSessionLocal() as session:
        # 1. Fetch column metadata
        columns_query = """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
        """
        params: dict[str, Any] = {}
        if table_name:
            columns_query += " AND table_name = :tname"
            params["tname"] = table_name
        columns_query += " ORDER BY table_name, ordinal_position;"

        col_res = await session.execute(text(columns_query), params)
        raw_cols = col_res.fetchall()

        schema_map: dict[str, list[dict[str, Any]]] = {}
        for r in raw_cols:
            t_name = r[0]
            if t_name not in schema_map:
                schema_map[t_name] = []
            schema_map[t_name].append(
                {
                    "column_name": r[1],
                    "data_type": r[2],
                    "is_nullable": r[3] == "YES",
                }
            )

        # 2. Fetch preview samples for each table
        samples: dict[str, list[dict[str, Any]]] = {}
        for t_name in schema_map.keys():
            sample_res = await session.execute(
                text(f"SELECT * FROM {t_name} LIMIT 3;")
            )
            samples[t_name] = [dict(row._mapping) for row in sample_res.fetchall()]

        return {
            "tables": schema_map,
            "samples": samples,
        }


@app.post("/tools/dry-run", tags=["Tools"])
async def dry_run_query(request: DryRunRequest) -> dict[str, Any]:
    """
    Simulates a query execution:
      - Validates AST (blocks destructive statements and unconstrained mutations).
      - Executes inside a transaction on staging_db and immediately issues ROLLBACK.
      - Returns preview rows and count of affected rows.
    """
    try:
        return await transaction_executor.execute_dry_run(request.sql_query)
    except ASTValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AST Security Violation: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dry run simulation error: {e}",
        ) from e


@app.post("/tools/execute", tags=["Tools"])
async def execute_approved_mutation(
    request: ExecuteMutationRequest,
) -> dict[str, Any]:
    """
    Executes an approved mutation with Safety Net 2 Pre-Images:
      - Snapshots original rows and stores them in governance_db.row_pre_images.
      - Executes mutation on staging_db and COMMITs.
      - Offloads tool logs to MinIO if payload exceeds 5KB.
    """
    try:
        return await transaction_executor.execute_mutation_with_pre_images(
            execution_id=request.execution_id,
            raw_sql=request.sql_query,
        )
    except ASTValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AST Security Violation: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Execution error: {e}",
        ) from e


@app.post("/tools/rollback", tags=["Tools"])
async def rollback_mutation(request: RollbackRequest) -> dict[str, Any]:
    """
    Rolls back an execution using pre-images:
      - Restores original row states in staging_db.
      - Updates audit status in governance_db to ROLLED_BACK.
    """
    try:
        return await rollback_executor.rollback_execution(
            execution_id=request.execution_id
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback error: {e}",
        ) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("fastmcp_server.server:app", host="0.0.0.0", port=8001, reload=True)

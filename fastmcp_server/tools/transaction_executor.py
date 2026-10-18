import json
import uuid
from typing import Any, Optional

import sqlglot
from sqlglot import exp
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.storage import CustomJSONEncoder, storage_manager
from backend.app.db.models import RowPreImage, ToolLog
from backend.app.db.session import AsyncGovSessionLocal, AsyncStagingSessionLocal
from fastmcp_server.ast_validator import ast_validator


class TransactionExecutor:
    """
    Executes approved database mutations inside transactional boundaries while
    capturing immutable pre-images in governance_db prior to modification.
    """

    def __init__(self) -> None:
        self.validator = ast_validator
        self.storage = storage_manager

    def _extract_table_and_where(self, validated_sql: str) -> tuple[str, str]:
        """
        Extracts the target table name and WHERE clause expression from a validated SQL query.
        """
        expression = sqlglot.parse_one(validated_sql, read="postgres")

        table_node = expression.find(exp.Table)
        if table_node is None:
            raise ValueError(f"Unable to resolve target table name in SQL: {validated_sql}")
        table_name = table_node.name

        where_node = expression.find(exp.Where)
        if where_node is None:
            raise ValueError(f"No WHERE condition found in query: {validated_sql}")
        where_sql = where_node.this.sql(dialect="postgres")

        return table_name, where_sql

    async def execute_dry_run(self, raw_sql: str) -> dict[str, Any]:
        """
        Simulates query execution in a safe sandbox:
          1. Validates AST.
          2. Inspects matching rows.
          3. Executes query in a transaction that always issues ROLLBACK.
        """
        sanitized_sql, is_read_only = self.validator.validate_and_transform(raw_sql)

        if is_read_only:
            async with AsyncStagingSessionLocal() as staging_session:
                result = await staging_session.execute(text(sanitized_sql))
                rows = [dict(r._mapping) for r in result.fetchall()]
                return {
                    "is_dry_run": True,
                    "is_read_only": True,
                    "matched_count": len(rows),
                    "preview_sample": rows[:3],
                }

        table_name, where_sql = self._extract_table_and_where(sanitized_sql)

        async with AsyncStagingSessionLocal() as staging_session:
            # 1. Fetch rows that would be affected
            fetch_sql = f"SELECT * FROM {table_name} WHERE {where_sql};"
            pre_result = await staging_session.execute(text(fetch_sql))
            matched_rows = [dict(r._mapping) for r in pre_result.fetchall()]

            # 2. Test execution, measure affected rows, then ROLLBACK
            mutation_result = await staging_session.execute(text(sanitized_sql))
            affected_count = mutation_result.rowcount
            await staging_session.rollback()

            return {
                "is_dry_run": True,
                "is_read_only": False,
                "target_table": table_name,
                "sanitized_sql": sanitized_sql,
                "matched_count": len(matched_rows),
                "simulated_affected_count": affected_count,
                "preview_sample": matched_rows[:3],
            }

    async def execute_mutation_with_pre_images(
        self,
        execution_id: uuid.UUID,
        raw_sql: str,
        primary_key_column: str = "id",
    ) -> dict[str, Any]:
        """
        Executes a validated mutation with Safety Net 2 Pre-Image snapshots:
          1. Validates SQL through AST parser.
          2. Fetches target rows and serializes their original state.
          3. Writes row pre-images permanently to governance_db.
          4. Executes the mutation against staging_db and COMMITs.
          5. Logs execution and offloads payloads > 5KB to MinIO.
        """
        sanitized_sql, is_read_only = self.validator.validate_and_transform(raw_sql)

        if is_read_only:
            raise ValueError(
                "execute_mutation_with_pre_images requires an UPDATE or DELETE statement. "
                "For read operations, use dry_run or inspect_schema."
            )

        table_name, where_sql = self._extract_table_and_where(sanitized_sql)

        async with AsyncStagingSessionLocal() as staging_session:
            # Step 1: Snapshot exact pre-images of target rows prior to change
            fetch_sql = f"SELECT * FROM {table_name} WHERE {where_sql};"
            pre_result = await staging_session.execute(text(fetch_sql))
            original_rows = [dict(r._mapping) for r in pre_result.fetchall()]

            if not original_rows:
                return {
                    "status": "NO_OP",
                    "message": "Query matched 0 rows. No state modified and no pre-images needed.",
                    "affected_rows": 0,
                    "target_table": table_name,
                }

            # Step 2: Save original row snapshots into governance_db.row_pre_images
            async with AsyncGovSessionLocal() as gov_session:
                for row in original_rows:
                    pk_val = str(row.get(primary_key_column, ""))
                    serializable_state = json.loads(
                        json.dumps(row, cls=CustomJSONEncoder)
                    )
                    pre_image = RowPreImage(
                        execution_id=execution_id,
                        target_table=table_name,
                        primary_key_value=pk_val,
                        pre_image_state=serializable_state,
                    )
                    gov_session.add(pre_image)
                await gov_session.commit()

            # Step 3: Execute mutation on staging_db and COMMIT
            mutation_result = await staging_session.execute(text(sanitized_sql))
            affected_count = mutation_result.rowcount
            await staging_session.commit()

            # Step 4: Handle tool logging and large payload offloading to MinIO
            log_payload = {
                "sanitized_sql": sanitized_sql,
                "affected_count": affected_count,
                "pre_images_count": len(original_rows),
                "original_rows_sample": original_rows[:3],
            }
            summary, s3_uri = self.storage.offload_if_exceeds_threshold(
                log_payload, file_prefix=f"exec_{execution_id}"
            )

            async with AsyncGovSessionLocal() as gov_session:
                tool_log = ToolLog(
                    execution_id=execution_id,
                    tool_name="fastmcp_execute_mutation",
                    input_parameters={"raw_sql": raw_sql, "sanitized_sql": sanitized_sql},
                    output_summary=f"Successfully modified {affected_count} rows in table '{table_name}'.",
                    payload_s3_uri=s3_uri,
                    is_dry_run=False,
                    execution_status="COMMITTED",
                )
                gov_session.add(tool_log)
                await gov_session.commit()

            return {
                "status": "COMMITTED",
                "target_table": table_name,
                "sanitized_sql": sanitized_sql,
                "affected_rows": affected_count,
                "pre_images_captured": len(original_rows),
                "payload_s3_uri": s3_uri,
            }


transaction_executor = TransactionExecutor()

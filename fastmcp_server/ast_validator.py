from typing import Optional
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


# ==============================================================================
# AST Security Exceptions
# ==============================================================================


class ASTValidationError(Exception):
    """Base exception for AST SQL validation failures."""

    pass


class SQLSyntaxError(ASTValidationError):
    """Raised when the input SQL cannot be parsed into a valid AST."""

    pass


class DestructiveSQLBlockedError(ASTValidationError):
    """Raised when destructive DDL or DML statements (DROP, TRUNCATE, ALTER) are detected."""

    pass


class UnconstrainedMutationError(ASTValidationError):
    """Raised when an UPDATE or DELETE statement lacks a valid, non-trivial WHERE clause."""

    pass


# ==============================================================================
# AST SQL Validator Engine
# ==============================================================================


class ASTValidator:
    """
    Abstract Syntax Tree (AST) Security Validator for PostgreSQL.
    Enforces that all queries executed by AI agents conform to strict enterprise
    safety guardrails:
      1. Blocks destructive statements (DROP, TRUNCATE, ALTER).
      2. Enforces mandatory, non-trivial WHERE clauses on UPDATE and DELETE.
      3. Converts hard DELETE queries to non-destructive soft updates.
    """

    PROHIBITED_EXPRESSIONS: tuple[type[exp.Expression], ...] = (
        exp.Drop,
        exp.Alter,
        exp.Command,
    )

    TRIVIAL_TAUTOLOGIES: set[str] = {
        "1 = 1",
        "1=1",
        "true",
        "'1' = '1'",
        "'a' = 'a'",
        "0 = 0",
        "0=0",
    }

    def validate_and_transform(self, sql_query: str) -> tuple[str, bool]:
        """
        Parses, validates, and optionally transforms a SQL statement.

        Returns:
            tuple[str, bool]: (sanitized_sql, is_read_only)
        Raises:
            SQLSyntaxError: If the SQL cannot be parsed.
            DestructiveSQLBlockedError: If DROP, TRUNCATE, or ALTER is present.
            UnconstrainedMutationError: If UPDATE or DELETE lacks a valid WHERE clause.
        """
        trimmed = sql_query.strip().rstrip(";")
        if not trimmed:
            raise SQLSyntaxError("SQL query is empty.")

        try:
            expressions = sqlglot.parse(trimmed, read="postgres")
        except ParseError as e:
            raise SQLSyntaxError(f"Failed to parse SQL into AST: {e}") from e

        if not expressions:
            raise SQLSyntaxError("No valid SQL expressions parsed.")

        if len(expressions) > 1:
            raise ASTValidationError(
                "Multi-statement SQL scripts are not permitted in a single tool call. "
                "Each query must be validated and executed individually."
            )

        root = expressions[0]
        if root is None:
            raise SQLSyntaxError("Parsed expression is null.")

        # 1. Prohibit Destructive Statements
        self._check_prohibited_statements(root, trimmed)

        # 2. Check for Read-Only SELECT
        if isinstance(root, exp.Select):
            return root.sql(dialect="postgres") + ";", True

        # 3. Enforce WHERE Clauses on UPDATE
        if isinstance(root, exp.Update):
            self._validate_where_clause(root, "UPDATE")
            return root.sql(dialect="postgres") + ";", False

        # 4. Enforce WHERE Clauses and Transform DELETE -> Soft Delete
        if isinstance(root, exp.Delete):
            self._validate_where_clause(root, "DELETE")
            transformed_update = self._convert_delete_to_soft_update(root)
            return transformed_update.sql(dialect="postgres") + ";", False

        # 5. Allow INSERT statements if bounded
        if isinstance(root, exp.Insert):
            return root.sql(dialect="postgres") + ";", False

        raise ASTValidationError(
            f"Unsupported statement type: {root.__class__.__name__}. "
            f"Only SELECT, INSERT, UPDATE, and soft-delete DELETE operations are permitted."
        )

    def _check_prohibited_statements(
        self, expression: exp.Expression, raw_sql: str
    ) -> None:
        """Inspects the AST for explicitly prohibited destructive statements."""
        # Check AST nodes
        for prohibited_type in self.PROHIBITED_EXPRESSIONS:
            if expression.find(prohibited_type) is not None or isinstance(
                expression, prohibited_type
            ):
                raise DestructiveSQLBlockedError(
                    f"Destructive operation blocked by AST policy: {prohibited_type.__name__}. "
                    f"Queries modifying schemas or dropping tables are forbidden."
                )

        # Explicit check for TRUNCATE keyword
        raw_upper = raw_sql.upper()
        if "TRUNCATE" in raw_upper:
            raise DestructiveSQLBlockedError(
                "Destructive operation blocked: TRUNCATE is strictly prohibited."
            )

    def _validate_where_clause(
        self, expression: exp.Expression, op_type: str
    ) -> None:
        """
        Validates that an UPDATE or DELETE statement has a non-empty,
        non-trivial WHERE clause.
        """
        where_node = expression.find(exp.Where)
        if where_node is None:
            raise UnconstrainedMutationError(
                f"Unconstrained {op_type} blocked: Query does not contain a WHERE clause! "
                f"All mutations must specify explicit scoping conditions."
            )

        where_sql = (
            where_node.this.sql(dialect="postgres").strip().lower()
            if where_node.this
            else ""
        )
        if not where_sql or where_sql in self.TRIVIAL_TAUTOLOGIES:
            raise UnconstrainedMutationError(
                f"Tautological WHERE clause rejected: 'WHERE {where_sql}'. "
                f"Mutations with always-true conditions (e.g. 1=1) are forbidden."
            )

    def _convert_delete_to_soft_update(self, delete_node: exp.Delete) -> exp.Update:
        """
        Transforms a DELETE statement into a soft-delete UPDATE:
          DELETE FROM table WHERE condition;
          -->
          UPDATE table SET is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP WHERE condition;
        """
        table = delete_node.this
        where_clause = delete_node.find(exp.Where)

        # Build SET expressions
        soft_delete_expr = sqlglot.parse_one("is_deleted = TRUE", read="postgres")
        timestamp_expr = sqlglot.parse_one(
            "deleted_at = CURRENT_TIMESTAMP", read="postgres"
        )

        update_node = exp.Update(
            this=table,
            expressions=[soft_delete_expr, timestamp_expr],
            where=where_clause,
        )
        return update_node


ast_validator = ASTValidator()

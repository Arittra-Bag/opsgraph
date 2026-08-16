"""Deterministic SELECT-only validation and broker protocol.

The alpha broker is defense in depth, not a substitute for a database account
whose server-side permissions are SELECT-only. Production connectors must also
set read-only transactions, statement timeouts, and network isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from opsgraph.domain import EvidenceArtifact, Obligation, Principal, QueryPlan
from opsgraph.domain.models import stable_hash
from opsgraph.policy import ActionRequest, FailClosedPolicy


class UnsafeQuery(ValueError):
    """Raised when SQL is outside the deliberately small safe subset."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


class ReadOnlyExecutor(Protocol):
    """Connector seam; implementations must enforce DB-native read-only mode."""

    def execute_readonly(self, sql: str, *, timeout_ms: int) -> QueryResult: ...


_MUTATING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE|REINDEX|"
    r"VACUUM|ANALYZE|CLUSTER|REFRESH|GRANT|REVOKE|COMMENT|SECURITY|COPY|CALL|DO|"
    r"LISTEN|NOTIFY|UNLISTEN|LOCK|SET|RESET|PREPARE|EXECUTE|DEALLOCATE|DISCARD)\b",
    re.IGNORECASE,
)
_SELECT_INTO = re.compile(r"\bSELECT\b[\s\S]*?\bINTO\b", re.IGNORECASE)
_ROW_LOCK = re.compile(r"\bFOR\s+(UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b", re.I)
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+(?!\()(?P<name>(?:\"[^\"]+\"|[A-Za-z_]\w*)"
    r"(?:\.(?:\"[^\"]+\"|[A-Za-z_]\w*))?)",
    re.IGNORECASE,
)
_FUNCTION = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\(")
_SAFE_FUNCTIONS = frozenset(
    {
        "avg",
        "cast",
        "coalesce",
        "count",
        "date_trunc",
        "exists",
        "extract",
        "greatest",
        "in",
        "least",
        "lower",
        "max",
        "min",
        "nullif",
        "round",
        "sum",
        "upper",
    }
)
_DANGEROUS_FUNCTIONS = frozenset(
    {
        "current_setting",
        "dblink",
        "lo_export",
        "lo_import",
        "pg_ls_dir",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_sleep",
        "pg_stat_file",
        "set_config",
    }
)

try:  # Optional at import time; validation fails closed when unavailable.
    from pglast import ast as _pg_ast
    from pglast import parse_sql as _parse_sql
    from pglast.visitors import Visitor as _PgVisitor
except ImportError:  # pragma: no cover - environment dependent
    _pg_ast = None
    _parse_sql = None
    _PgVisitor = object


class _ScopeVisitor(_PgVisitor):
    """Collect relations/functions and reject non-read AST nodes."""

    _FORBIDDEN_NODES = frozenset(
        {
            "AlterTableStmt",
            "CallStmt",
            "CopyStmt",
            "CreateStmt",
            "DeleteStmt",
            "DoStmt",
            "GrantStmt",
            "InsertStmt",
            "LockStmt",
            "MergeStmt",
            "RefreshMatViewStmt",
            "TransactionStmt",
            "TruncateStmt",
            "UpdateStmt",
            "VariableSetStmt",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[str] = []
        self.ctes: set[str] = set()
        self.functions: set[str] = set()
        self.unqualified_tables: set[str] = set()
        self.invalid_identifier: str | None = None
        self.namespaced_function: str | None = None
        self.namespaced_operator: str | None = None
        self.namespaced_type: str | None = None
        self.forbidden_node: str | None = None

    def visit(self, ancestors, node):  # noqa: ANN001 - pglast visitor protocol
        del ancestors
        node_name = type(node).__name__
        if node_name in self._FORBIDDEN_NODES:
            self.forbidden_node = node_name

    def visit_RangeVar(self, ancestors, node):  # noqa: ANN001,N802 - pglast node name
        del ancestors
        relation = str(node.relname)
        if "." in relation:
            self.invalid_identifier = relation
        if node.schemaname:
            schema = str(node.schemaname)
            if "." in schema:
                self.invalid_identifier = schema
            relation = f"{schema}.{relation}"
        else:
            self.unqualified_tables.add(relation)
        self.tables.append(relation)

    def visit_CommonTableExpr(  # noqa: ANN001,N802 - pglast node name
        self, ancestors, node
    ):
        del ancestors
        self.ctes.add(str(node.ctename))

    def visit_FuncCall(self, ancestors, node):  # noqa: ANN001,N802 - pglast node name
        del ancestors
        parts = []
        for part in node.funcname:
            value = getattr(part, "sval", getattr(part, "val", None))
            if value:
                parts.append(str(value))
        if parts:
            if len(parts) != 1:
                self.namespaced_function = ".".join(parts)
            self.functions.add(parts[-1].lower())

    def visit_A_Expr(self, ancestors, node):  # noqa: ANN001,N802 - pglast node name
        del ancestors
        parts = [
            str(value)
            for part in (node.name or ())
            if (value := getattr(part, "sval", getattr(part, "val", None)))
        ]
        if len(parts) > 1 and parts[0].lower() != "pg_catalog":
            self.namespaced_operator = ".".join(parts)

    def visit_TypeName(self, ancestors, node):  # noqa: ANN001,N802 - pglast node name
        del ancestors
        parts = [
            str(value)
            for part in (node.names or ())
            if (value := getattr(part, "sval", getattr(part, "val", None)))
        ]
        # PostgreSQL normalizes some built-in aliases (for example integer) to
        # pg_catalog names. Any other explicit namespace may invoke a custom
        # cast function and is outside the alpha's auditable SQL subset.
        if len(parts) > 1 and parts[0].lower() != "pg_catalog":
            self.namespaced_type = ".".join(parts)


class SelectOnlyValidator:
    """Validate one SELECT/CTE statement and attach bounded obligations."""

    def validate(self, *, workspace_id: str, sql: str, obligations: Obligation) -> QueryPlan:
        if not sql or len(sql) > 50_000:
            raise UnsafeQuery("query must contain 1-50000 characters")
        if "--" in sql or "/*" in sql or "*/" in sql or "$$" in sql:
            raise UnsafeQuery("comments and dollar-quoted strings are not supported")
        normalized = self._normalize(sql)
        masked = self._mask_literals(normalized)
        if ";" in masked:
            raise UnsafeQuery("stacked statements are not allowed")
        if not re.match(r"^(SELECT|WITH)\b", masked, re.IGNORECASE):
            raise UnsafeQuery("only SELECT statements are allowed")
        if _MUTATING.search(masked):
            raise UnsafeQuery("query contains a mutating or administrative keyword")
        if _SELECT_INTO.search(masked):
            raise UnsafeQuery("SELECT INTO is not allowed")
        if _ROW_LOCK.search(masked):
            raise UnsafeQuery("row locks are not allowed")
        tables, functions = self._ast_scope(normalized)
        dangerous = functions & _DANGEROUS_FUNCTIONS
        if dangerous:
            raise UnsafeQuery(f"dangerous function is not allowed: {sorted(dangerous)[0]}")
        unknown = functions - _SAFE_FUNCTIONS
        if unknown:
            raise UnsafeQuery(f"function is not allowlisted: {sorted(unknown)[0]}")
        if not tables:
            raise UnsafeQuery("query must reference at least one table")
        self._enforce_table_scope(tables, obligations)
        bounded = (
            f"SELECT * FROM ({normalized}) AS _opsgraph_bounded LIMIT {obligations.max_rows + 1}"  # noqa: S608,E501 -- strict validator above.
        )
        fingerprint = stable_hash(
            {
                "workspace_id": workspace_id,
                "sql": normalized,
                "obligations": obligations.model_dump(mode="json"),
            }
        )
        return QueryPlan(
            workspace_id=workspace_id,
            sql=bounded,
            normalized_sql=normalized,
            referenced_tables=tables,
            obligations=obligations,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _ast_scope(sql: str) -> tuple[tuple[str, ...], set[str]]:
        if _parse_sql is None or _pg_ast is None:
            raise UnsafeQuery("PostgreSQL AST parser is unavailable")
        try:
            statements = _parse_sql(sql)
        except Exception:
            raise UnsafeQuery("query is not valid PostgreSQL") from None
        if len(statements) != 1 or not isinstance(statements[0].stmt, _pg_ast.SelectStmt):
            raise UnsafeQuery("exactly one SELECT statement is required")
        statement = statements[0].stmt
        if statement.intoClause:
            raise UnsafeQuery("SELECT INTO is not allowed")
        if statement.lockingClause:
            raise UnsafeQuery("row locks are not allowed")
        visitor = _ScopeVisitor()
        visitor(statement)
        if visitor.forbidden_node:
            raise UnsafeQuery("query contains a non-read operation")
        if visitor.invalid_identifier:
            raise UnsafeQuery("quoted identifiers containing dots are not supported")
        if visitor.namespaced_function:
            raise UnsafeQuery("schema-qualified functions are not allowed")
        if visitor.namespaced_operator:
            raise UnsafeQuery("custom schema-qualified operators are not allowed")
        if visitor.namespaced_type:
            raise UnsafeQuery("custom schema-qualified types are not allowed")
        unqualified = visitor.unqualified_tables - visitor.ctes
        if unqualified:
            raise UnsafeQuery(
                f"table references must be schema-qualified: {sorted(unqualified)[0]}"
            )
        tables = tuple(
            dict.fromkeys(
                table for table in visitor.tables if "." in table or table not in visitor.ctes
            )
        )
        return tables, visitor.functions

    @staticmethod
    def _normalize(sql: str) -> str:
        sql = sql.strip()
        if sql.endswith(";"):
            sql = sql[:-1].rstrip()
        output: list[str] = []
        index = 0
        whitespace = False
        while index < len(sql):
            char = sql[index]
            if char == "'":
                if whitespace and output:
                    output.append(" ")
                whitespace = False
                output.append(char)
                index += 1
                while index < len(sql):
                    output.append(sql[index])
                    if sql[index] == "'":
                        if index + 1 < len(sql) and sql[index + 1] == "'":
                            output.append(sql[index + 1])
                            index += 2
                            continue
                        index += 1
                        break
                    index += 1
                else:
                    raise UnsafeQuery("unterminated string literal")
                continue
            if char.isspace():
                whitespace = True
            else:
                if whitespace and output:
                    output.append(" ")
                whitespace = False
                output.append(char)
            index += 1
        return "".join(output)

    @staticmethod
    def _mask_literals(sql: str) -> str:
        result: list[str] = []
        index = 0
        while index < len(sql):
            if sql[index] != "'":
                result.append(sql[index])
                index += 1
                continue
            result.append("''")
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise UnsafeQuery("unterminated string literal")
        return "".join(result)

    @staticmethod
    def _table_name(value: str) -> str:
        return ".".join(part.strip('"') for part in value.split("."))

    @staticmethod
    def _enforce_table_scope(tables: tuple[str, ...], obligations: Obligation) -> None:
        allowed_schemas = set(obligations.allowed_schemas)
        allowed_tables = set(obligations.allowed_tables)
        for table in tables:
            parts = table.split(".")
            schema = parts[0] if len(parts) == 2 else "public"
            bare = parts[-1]
            qualified = f"{schema}.{bare}"
            if schema not in allowed_schemas:
                raise UnsafeQuery(f"schema is outside policy scope: {schema}")
            if allowed_tables and bare not in allowed_tables and qualified not in allowed_tables:
                raise UnsafeQuery(f"table is outside policy scope: {qualified}")


class QueryBroker:
    """Authorize, validate, execute, bound, and hash a read-only query."""

    def __init__(
        self,
        *,
        policy: FailClosedPolicy,
        validator: SelectOnlyValidator,
        executor: ReadOnlyExecutor,
    ) -> None:
        self.policy = policy
        self.validator = validator
        self.executor = executor

    def query(self, *, principal: Principal, sql: str) -> EvidenceArtifact:
        decision = self.policy.authorize(
            ActionRequest(
                principal=principal,
                action="core.query.read",
                workspace_id=principal.workspace_id,
                resource="database",
            )
        )
        if not decision.allowed or decision.obligations is None:
            raise PermissionError(decision.reason)
        plan = self.validator.validate(
            workspace_id=principal.workspace_id,
            sql=sql,
            obligations=decision.obligations,
        )
        result = self.executor.execute_readonly(plan.sql, timeout_ms=plan.obligations.timeout_ms)
        limit = plan.obligations.max_rows
        rows = result.rows[:limit]
        return EvidenceArtifact.from_result(
            workspace_id=principal.workspace_id,
            query_fingerprint=plan.fingerprint,
            referenced_tables=plan.referenced_tables,
            columns=result.columns,
            rows=rows,
            truncated=len(result.rows) > limit,
        )

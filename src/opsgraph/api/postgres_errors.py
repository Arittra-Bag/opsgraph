"""Structured PostgreSQL diagnostics with a fixed public response."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from opsgraph.postgres_diagnostics import connection_diagnostic


class PostgresHTTPError(HTTPException):
    def __init__(self, code: str, *, status_code: int = 422):
        self.diagnostic = connection_diagnostic(code).as_dict()
        super().__init__(status_code=status_code, detail=self.diagnostic["message"])


def postgres_error_response(_request: Request, error: PostgresHTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "diagnostic": error.diagnostic},
        headers={"Cache-Control": "no-store"},
    )

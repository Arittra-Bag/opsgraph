"""Credential-safe PostgreSQL failure categories and repair guidance."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ConnectionDiagnostic:
    code: str
    title: str
    message: str
    steps: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


_DIAGNOSTICS = {
    item.code: item
    for item in (
        ConnectionDiagnostic(
            "schema_too_large",
            "Inspection scope too large",
            "Schema metadata exceeds the bounded inspection limit.",
            (
                "Select fewer exact tables and inspect again.",
                "Split large databases into separately approved sources.",
            ),
        ),
        ConnectionDiagnostic(
            "scope_metadata_incomplete",
            "Inspection metadata incomplete",
            "The selected table metadata could not be recorded consistently.",
            (
                "Review SELECT and schema USAGE grants for the exact selected tables.",
                "Check for concurrent schema changes, then explicitly inspect again.",
            ),
        ),
        ConnectionDiagnostic(
            "connection_failed",
            "Connection unavailable",
            "PostgreSQL connection failed. No database credentials or driver details are shown.",
            (
                "Verify the endpoint from the OpsGraph backend host.",
                "Check the hosting guide, database availability, and dedicated read-only login.",
                "Correct the configuration, then explicitly inspect again.",
            ),
        ),
        ConnectionDiagnostic(
            "invalid_configuration",
            "Connection settings need attention",
            "PostgreSQL connection settings are invalid.",
            (
                "Use a PostgreSQL URL or libpq string with an explicit "
                "host, port, database and user.",
                "Enter credentials privately on the backend, never in a source display name.",
            ),
        ),
        ConnectionDiagnostic(
            "tls_required",
            "Verified TLS required",
            "Remote PostgreSQL requires sslmode=verify-full.",
            (
                "Use the database hostname and sslmode=verify-full in the "
                "private connection string.",
                "Configure the hosting provider's trusted CA on the backend filesystem.",
                "Keep hostname and certificate verification enabled.",
            ),
        ),
        ConnectionDiagnostic(
            "tls_verification",
            "Certificate verification failed",
            "The PostgreSQL certificate or hostname could not be verified.",
            (
                "Use the provider's exact database hostname, not a replacement IP address.",
                "Check the trusted CA path, certificate validity, and backend clock.",
                "For containers, mount the CA file where the connection string expects it.",
            ),
        ),
        ConnectionDiagnostic(
            "tls_configuration",
            "TLS configuration unavailable",
            "PostgreSQL TLS configuration could not be loaded or negotiated.",
            (
                "Check that the CA and any client certificate files exist "
                "and are readable privately.",
                "Verify the endpoint accepts PostgreSQL TLS and use the provider's current CA.",
                "Do not disable certificate verification to bypass this failure.",
            ),
        ),
        ConnectionDiagnostic(
            "dns_failed",
            "Hostname could not be resolved",
            "The PostgreSQL hostname could not be resolved from the backend.",
            (
                "Check the endpoint spelling and backend DNS or VPN configuration.",
                "For Supabase, check IPv6 reachability or use the documented session pooler.",
            ),
        ),
        ConnectionDiagnostic(
            "network_unavailable",
            "Database endpoint unreachable",
            "The backend could not reach the PostgreSQL endpoint.",
            (
                "Check host and port, database availability, firewall "
                "rules, and network allowlists.",
                "Private endpoints require a reachable VPN, VPC route, or "
                "separately managed proxy.",
                "Inside Docker, localhost refers to the OpsGraph container.",
            ),
        ),
        ConnectionDiagnostic(
            "connection_timeout",
            "Connection timed out",
            "The PostgreSQL connection did not complete within its timeout.",
            (
                "Check network reachability and provider availability or serverless wake-up state.",
                "Confirm the endpoint and port before explicitly retrying.",
            ),
        ),
        ConnectionDiagnostic(
            "authentication_failed",
            "Database login rejected",
            "PostgreSQL rejected the configured database login.",
            (
                "Check the dedicated role name and password privately on the backend.",
                "Poolers may require a provider-specific username format.",
                "Renew expiring credentials through your existing operator workflow.",
            ),
        ),
        ConnectionDiagnostic(
            "access_denied",
            "Database access denied",
            "PostgreSQL denied access for this connection or operation.",
            (
                "Review network admission, database CONNECT, schema USAGE, "
                "and exact SELECT grants.",
                "Ask the database administrator to review the least-privilege guide.",
                "Do not substitute an administrator login.",
            ),
        ),
        ConnectionDiagnostic(
            "database_missing",
            "Database name not found",
            "The configured PostgreSQL database was not found.",
            (
                "Check the database name in the private connection string.",
                "Use the database endpoint, not a hosting dashboard or HTTP API URL.",
            ),
        ),
        ConnectionDiagnostic(
            "capacity_unavailable",
            "Database temporarily unavailable",
            "PostgreSQL cannot accept this connection at present.",
            (
                "Check provider status and connection limits.",
                "Wait for the database to become available, then explicitly inspect again.",
            ),
        ),
        ConnectionDiagnostic(
            "query_timeout",
            "Bounded database operation timed out",
            "The database operation exceeded its configured time limit.",
            (
                "Narrow the question, approved scope, or selected columns.",
                "Review locks and database load with your administrator.",
                "Earlier evidence remains attached to its original attempt.",
            ),
        ),
        ConnectionDiagnostic(
            "unsafe_role",
            "Read-only role required",
            "The database role is not safely read-only.",
            (
                "Use a dedicated SELECT-only role with no elevated or effective write privileges.",
                "Review inherited and PUBLIC grants, including privileges outside selected tables.",
                "Generate the role guide, have it reviewed, then inspect again.",
            ),
        ),
        ConnectionDiagnostic(
            "scope_unavailable",
            "Approved tables unavailable",
            "An approved relation is missing, inaccessible, or outside the permitted scope.",
            (
                "Check exact schema-qualified table names and schema USAGE plus SELECT grants.",
                "Review partition or inheritance descendants and configured playbook mappings.",
                "Save and inspect the corrected scope, then approve readiness again.",
            ),
        ),
        ConnectionDiagnostic(
            "credential_missing",
            "Backend connection not configured",
            "The approved database credential reference is not configured on the backend.",
            (
                "Use the private launcher configuration or your approved "
                "environment-variable workflow.",
                "Restart the backend after changing its environment, then inspect the source.",
                "Never paste a connection URL into browser source fields.",
            ),
        ),
    )
}


def connection_diagnostic(code: str) -> ConnectionDiagnostic:
    """Only fixed, reviewed content may cross the public error boundary."""
    return _DIAGNOSTICS.get(code, _DIAGNOSTICS["connection_failed"])


def classify_postgres_failure(error: Exception, *, connecting: bool = False) -> str:
    """Inspect driver details locally, returning a fixed category without those details."""
    state = getattr(error, "sqlstate", None)
    if state == "28P01":
        return "authentication_failed"
    if state in {"28000", "42501"}:
        return "access_denied"
    if state == "3D000":
        return "database_missing"
    if state in {"53300", "57P03"}:
        return "capacity_unavailable"
    if state == "57014":
        return "query_timeout"
    # libpq connection failures commonly have no SQLSTATE. Never expose this text.
    message = str(error).lower()[:4096] if connecting else ""
    if "password authentication failed" in message or "no password supplied" in message:
        return "authentication_failed"
    if "no pg_hba.conf entry" in message:
        return "access_denied"
    if any(
        value in message
        for value in (
            "certificate verify failed",
            "does not match host name",
            "hostname mismatch",
            "certificate has expired",
            "self-signed certificate",
        )
    ):
        return "tls_verification"
    if any(
        value in message
        for value in (
            "root certificate file",
            "could not read certificate",
            "ssl error",
            "does not support ssl",
            "could not load private key",
            "private key file",
        )
    ):
        return "tls_configuration"
    if "could not translate host name" in message or "name or service not known" in message:
        return "dns_failed"
    if "timeout" in message or "timed out" in message:
        return "connection_timeout"
    if any(
        value in message
        for value in (
            "connection refused",
            "network is unreachable",
            "no route to host",
        )
    ):
        return "network_unavailable"
    return "connection_failed"

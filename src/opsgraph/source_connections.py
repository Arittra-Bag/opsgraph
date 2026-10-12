"""Shared private source configuration, separate from source records and evidence."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import ssl
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opsgraph.setup import (
    SetupError,
    ensure_private_directory,
    normalize_guided_dsn,
    read_private_config,
    write_private_config,
)

REFERENCE = re.compile(r"^OPSGRAPH_[A-Z0-9_]{1,100}_DSN$")


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    secret_ref: str | None = Field(default=None, pattern=REFERENCE.pattern)
    dsn: SecretStr = Field(repr=False)
    ca_pem: str = Field(default="", max_length=65_536, repr=False)


def connection_directory(settings) -> Path:
    return settings.state_path.parent / (settings.state_path.name + ".sources")


def connection_path(settings, reference: str) -> Path:
    if not REFERENCE.fullmatch(reference):
        raise SetupError("Choose an approved OPSGRAPH_*_DSN reference.")
    return connection_directory(settings) / (reference + ".env")


def credential_fingerprint(dsn: str, binding_key: str) -> str:
    return hmac.new(
        binding_key.encode(), b"opsgraph-source-connection-v1\0" + dsn.encode(), hashlib.sha256
    ).hexdigest()


def _receipt(runtime, reference: str):
    for entry in reversed(runtime.audit.entries):
        if (
            entry.workspace_id == runtime.settings.workspace_id
            and entry.action == "core.source.manage"
            and entry.resource == reference
            and entry.outcome == "allowed"
            and entry.details.get("reason") == "connection_saved"
        ):
            return entry
    return None


def private_connection(runtime, reference: str) -> dict | None:
    path = connection_path(runtime.settings, reference)
    receipt = _receipt(runtime, reference)
    if not path.exists() and receipt is None:
        return None
    try:
        values = read_private_config(path)
    except OSError:
        raise SetupError("Private connection storage is unavailable. Save it again.") from None
    try:
        valid = (
            receipt is not None
            and values["REVISION"] == receipt.details.get("revision")
            and hmac.compare_digest(
                credential_fingerprint(values["DSN"], values["BINDING_KEY"]),
                receipt.details.get("connection_fingerprint", ""),
            )
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise SetupError("Saved source connection has no matching receipt. Save it again.")
    return values


def approved_references(runtime) -> set[str]:
    references = set(runtime.settings.allowed_postgres_secret_refs)
    if runtime.settings.postgres_secret_ref:
        references.add(runtime.settings.postgres_secret_ref)
    directory = connection_directory(runtime.settings)
    if directory.exists():
        for path in directory.glob("*.env"):
            reference = path.stem
            if REFERENCE.fullmatch(reference) and private_connection(runtime, reference):
                references.add(reference)
    return references


def resolve_connection(runtime, reference: str) -> str | None:
    """An attested private connection takes precedence; environment setups still work."""
    values = private_connection(runtime, reference)
    if values is not None:
        params = conninfo_to_dict(values["DSN"])
        root = params.get("sslrootcert", "")
        if root.startswith("@opsgraph/"):
            name = root.removeprefix("@opsgraph/")
            if not re.fullmatch(r"[a-f0-9]{64}\.crt", name):
                raise SetupError("Saved source certificate needs attention.")
            path = connection_directory(runtime.settings) / "certificates" / name
            from opsgraph.maintenance import _safe_path

            _safe_path(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != name.removesuffix(".crt"):
                raise SetupError("Saved source certificate changed. Save it again.")
            params["sslrootcert"] = str(path)
        return make_conninfo(**params)
    return os.getenv(reference)


def connection_revision(runtime, reference: str) -> str | None:
    values = private_connection(runtime, reference)
    return values["REVISION"] if values else None


def portable_connection(dsn: str, *, supplied_ca: bool) -> str:
    """Attest a stable certificate identity so offline backups can be restored elsewhere."""
    if not supplied_ca:
        return dsn
    params = conninfo_to_dict(dsn)
    params["sslrootcert"] = "@opsgraph/" + Path(params["sslrootcert"]).name
    return make_conninfo(**params)


def prepare_connection(dsn: str, *, ca_pem: str = "", directory: Path | None = None) -> str:
    """Validate the same DSN in TUI/API, optionally store a content-addressed CA."""
    if not dsn or len(dsn) > 16_384:
        raise SetupError("Enter a complete PostgreSQL connection string of at most 16 KB.")
    if ca_pem:
        if directory is None or len(ca_pem) > 65_536:
            raise SetupError("Choose a PEM CA certificate of at most 64 KB.")
        try:
            if "PRIVATE KEY" in ca_pem or "BEGIN CERTIFICATE" not in ca_pem:
                raise ValueError
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=ca_pem)
            params = conninfo_to_dict(dsn)
        except Exception:
            raise SetupError(
                "Use a valid PostgreSQL connection and PEM CA certificates, never a private key."
            ) from None
        directory = ensure_private_directory(directory)
        path = directory / (hashlib.sha256(ca_pem.encode()).hexdigest() + ".crt")
        if path.exists() or path.is_symlink():
            # Validate ownership, links and permissions before using an existing CA.
            from opsgraph.maintenance import _safe_path

            _safe_path(path)
            if path.read_bytes() != ca_pem.encode():
                raise SetupError("The saved CA certificate changed. Choose another certificate.")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(ca_pem.encode())
        params["sslrootcert"] = str(path)
        dsn = make_conninfo(**params)
    return normalize_guided_dsn(dsn)


__all__ = ["ConnectionRequest", "prepare_connection", "read_private_config", "write_private_config"]

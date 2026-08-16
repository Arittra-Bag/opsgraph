"""Local development authentication.

This module intentionally does not claim SSO, session management, MFA, token
rotation, or enterprise readiness. It is disabled unless the operator opts in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from opsgraph.domain import Principal


@dataclass(frozen=True, slots=True)
class LocalDevCredential:
    subject: str
    workspace_id: str
    token_hash: str
    salt: str
    roles: frozenset[str]


class LocalDevAuthenticator:
    """Hash local bearer tokens and compare them in constant time."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self._credentials: dict[str, LocalDevCredential] = {}

    def issue(
        self,
        *,
        subject: str,
        workspace_id: str,
        roles: frozenset[str] = frozenset({"admin"}),
    ) -> tuple[str, LocalDevCredential]:
        if not self.enabled:
            raise PermissionError("local development authentication is disabled")
        token = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        digest = self._hash(token, salt)
        credential = LocalDevCredential(subject, workspace_id, digest, salt, roles)
        self._credentials[digest] = credential
        return token, credential

    def authenticate(self, token: str) -> Principal:
        if not self.enabled or not token:
            raise PermissionError("authentication failed")
        for credential in self._credentials.values():
            candidate = self._hash(token, credential.salt)
            if hmac.compare_digest(candidate, credential.token_hash):
                return Principal(
                    subject=credential.subject,
                    workspace_id=credential.workspace_id,
                    roles=credential.roles,
                )
        raise PermissionError("authentication failed")

    @staticmethod
    def _hash(token: str, salt: str) -> str:
        return hashlib.scrypt(
            token.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32
        ).hex()

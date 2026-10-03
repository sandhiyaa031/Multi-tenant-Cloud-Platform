from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import org_tx
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)
ROLE_RANK = {"VIEWER": 0, "OPERATOR": 1, "ADMIN": 2}


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    org_id: UUID
    role: str

    def tx(self):
        """A transaction bound to this principal; RLS applies to everything in it."""
        return org_tx(self.user_id, self.org_id)


async def get_principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        user_id, org_id = decode_access_token(creds.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")

    async with org_tx(user_id, org_id) as conn:
        cur = await conn.execute(
            """
            SELECT m.role::text AS role
            FROM cp.memberships m
            JOIN cp.users u ON u.id = m.user_id
            WHERE m.org_id = cp.current_org() AND m.user_id = cp.current_user_id() AND u.is_active
            """
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no active membership in this organization")
    return Principal(user_id=user_id, org_id=org_id, role=row["role"])


def require_role(minimum: str) -> Callable[..., Awaitable[Principal]]:
    """Endpoint guard. The database re-checks the same rule through RLS, so the
    two layers must both be wrong for a write to slip through."""

    async def guard(principal: Principal = Depends(get_principal)) -> Principal:
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{minimum} role required")
        return principal

    return guard

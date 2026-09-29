"""What browser authentication needs from the outside world.

Implementations live in `app/infrastructure/`; `app/bootstrap/auth.py` wires them.
"""

from __future__ import annotations

from typing import Any, Protocol


class IdentityProvider(Protocol):
    """The OIDC provider, already bound to one browser surface."""

    async def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        mode: str = "login",
    ) -> str: ...

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> dict[str, Any]:
        """Return the claims of the verified identity token."""
        ...


class SessionStore(Protocol):
    """Expiring browser state: login transactions and sessions."""

    async def create(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        """Store only if the key is absent. False means it already existed."""
        ...

    async def read(self, key: str) -> str | None: ...

    async def take(self, key: str) -> str | None:
        """Read and delete in one step, so a value can be used once."""
        ...

    async def delete(self, key: str) -> None: ...


class UserDirectory(Protocol):
    """Local users, keyed by the provider's issuer and subject."""

    async def upsert(
        self,
        *,
        issuer: str,
        subject: str,
        username: str,
        email: str | None,
        display_name: str | None,
        roles: list[str],
        scopes: list[str],
    ) -> dict[str, Any]:
        """Create or refresh; return id, email, display_name, roles, scopes."""
        ...

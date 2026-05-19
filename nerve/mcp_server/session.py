"""Satellite session resolver — map MCP connections to Nerve sessions.

Each MCP connection (one per ``mcp-session-id`` header in HTTP transport)
gets a corresponding row in the ``sessions`` table with ``source="external"``,
so external tool calls show up in the UI's session list alongside native
sessions. The session ID is deterministic from
``(client_name, client_session_id_or_mcp_session_id)`` so re-resolves are
idempotent — useful for tests and for clients that reconnect with the
same session id.

No DB migration is needed: the satellite is just a regular ``sessions``
row with ``source="external"`` and ``metadata`` JSON carrying the client
name, the raw mcp-session-id, and an optional client-supplied id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nerve.db import Database

logger = logging.getLogger(__name__)


def _sanitize_client_name(name: str | None) -> str:
    """Coerce an arbitrary client identifier into a session-id-safe slug.

    Session IDs are used in URLs and filesystem paths, so we restrict
    them to ``[A-Za-z0-9_.-]`` plus colons (which we use as separators).
    Anything else becomes ``_``.
    """
    if not name:
        return "external"
    safe = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in name)
    return safe or "external"


class SatelliteSessionResolver:
    """Resolve an MCP connection to a Nerve satellite session record.

    Constructed once per HTTP mount. Each ``resolve()`` call ensures a
    session row exists in the DB and returns its id. Callers should cache
    the result for the lifetime of the underlying MCP connection so we
    don't hit the DB per tool call.
    """

    def __init__(self, db: "Database") -> None:
        self.db = db

    @staticmethod
    def build_session_id(client_name: str, identifier: str) -> str:
        """Build the canonical satellite session id.

        Format: ``external:<client_name>:<identifier>``.

        ``identifier`` is either a client-supplied stable id (preferred —
        survives reconnects) or the per-connection mcp-session-id
        (best-effort fallback).
        """
        return f"external:{_sanitize_client_name(client_name)}:{identifier}"

    async def resolve(
        self,
        *,
        client_name: str | None,
        mcp_session_id: str,
        client_session_id: str | None = None,
    ) -> str:
        """Return the satellite session id, creating the row if needed.

        Args:
            client_name: From the MCP ``initialize`` request's
                ``clientInfo.name`` field, e.g. ``"codex"``, ``"claude-code"``.
                ``None`` is tolerated and mapped to ``"external"``.
            mcp_session_id: The transport-level session id (HTTP
                ``mcp-session-id`` header). Always present.
            client_session_id: Optional stable id supplied by the client
                (e.g. a Codex thread id). When provided, the satellite
                session id is stable across reconnects.
        """
        safe_client = _sanitize_client_name(client_name)
        identifier = client_session_id or mcp_session_id
        sid = self.build_session_id(safe_client, identifier)

        existing = await self.db.get_session(sid)
        if existing is not None:
            return sid

        metadata = {
            "client_name": safe_client,
            "mcp_session_id": mcp_session_id,
            "client_session_id": client_session_id,
            "runtime": f"{safe_client}-external",
        }
        title = f"{safe_client} ({mcp_session_id[:8]})"
        try:
            await self.db.create_session(
                session_id=sid,
                title=title,
                source="external",
                metadata=metadata,
                status="active",
            )
            logger.info(
                "Created satellite session %s (client=%s, mcp=%s)",
                sid, safe_client, mcp_session_id,
            )
        except Exception:
            # Race: another concurrent request created the row between
            # get_session() and create_session(). create_session() is
            # INSERT OR IGNORE so this is normally swallowed; the
            # broader except is belt-and-braces.
            logger.exception("Failed to create satellite session %s", sid)

        return sid

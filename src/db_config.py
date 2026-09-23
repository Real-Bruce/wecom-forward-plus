"""PostgreSQL-backed group configuration.

The ``groups`` table is the source of truth when ``CONFIG_SOURCE=database``.
:class:`GroupRepository` is a thin asyncpg wrapper (plain SQL, no ORM); the
row mapping, validation and diffing logic live in pure functions so they can
be tested without a real database.

Security: the DSN contains database credentials and the rows contain robot
secrets and Dify API keys — none of them may ever be logged. Validation
errors carry the group name and field name only, never values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import asyncpg

from .config import GroupConfig

logger = logging.getLogger(__name__)

# Sentinel distinguishing "field not provided" from an explicit None (= NULL,
# inherit the global default) on partial updates.
_UNSET = object()

_MAX_NAME_LENGTH = 100

_GROUP_COLUMNS = (
    "id, name, wecom_robot_id, wecom_robot_secret, dify_api_key, "
    "session_max_total, session_ttl_seconds, enabled, created_at, updated_at"
)

DDL = f"""
CREATE TABLE IF NOT EXISTS groups (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    wecom_robot_id      TEXT NOT NULL,
    wecom_robot_secret  TEXT NOT NULL,
    dify_api_key        TEXT NOT NULL,
    session_max_total   INTEGER,
    session_ttl_seconds INTEGER,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class GroupDefaults:
    """Global session parameters a nullable column falls back to."""

    session_ttl_seconds: int
    session_max_total: int


class GroupValidationError(Exception):
    """A group's fields are invalid; ``field`` names the offending one."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


def validate_group_fields(
    *,
    name: str,
    wecom_robot_id: str,
    wecom_robot_secret: str,
    dify_api_key: str,
    session_max_total: Optional[int],
    session_ttl_seconds: Optional[int],
) -> None:
    """Validate one group's fields.

    Error messages contain field names only, never the values (some of which
    are secrets). Shared by the database read path and the admin write path so
    a group accepted through the API can never break the reconcile loop.
    """
    if not (name or "").strip():
        raise GroupValidationError("name", "name must not be empty")
    if len(name) > _MAX_NAME_LENGTH:
        raise GroupValidationError("name", f"name must be at most {_MAX_NAME_LENGTH} characters")
    if not (wecom_robot_id or "").strip():
        raise GroupValidationError("wecom_robot_id", "wecom_robot_id must not be empty")
    if not (wecom_robot_secret or "").strip():
        raise GroupValidationError("wecom_robot_secret", "wecom_robot_secret must not be empty")
    if not (dify_api_key or "").strip():
        raise GroupValidationError("dify_api_key", "dify_api_key must not be empty")
    if session_max_total is not None and session_max_total <= 0:
        raise GroupValidationError("session_max_total", "session_max_total must be positive")
    if session_ttl_seconds is not None and session_ttl_seconds <= 0:
        raise GroupValidationError("session_ttl_seconds", "session_ttl_seconds must be positive")


def rows_to_desired(
    rows: Sequence[Mapping[str, Any]], defaults: GroupDefaults
) -> Dict[str, GroupConfig]:
    """Map database rows to the desired group set (enabled rows only).

    Raises :class:`GroupValidationError` (wrapped with the group name) on any
    invalid row so callers can fail fast or skip the reload cycle.
    """
    desired: Dict[str, GroupConfig] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        try:
            validate_group_fields(
                name=name,
                wecom_robot_id=str(row.get("wecom_robot_id") or ""),
                wecom_robot_secret=str(row.get("wecom_robot_secret") or ""),
                dify_api_key=str(row.get("dify_api_key") or ""),
                session_max_total=row.get("session_max_total"),
                session_ttl_seconds=row.get("session_ttl_seconds"),
            )
        except GroupValidationError as exc:
            label = name or "<unnamed row>"
            raise GroupValidationError(
                exc.field, f"group {label!r}: {exc}"
            ) from exc

        if row.get("enabled") is False:
            continue

        if name in desired:
            raise GroupValidationError("name", f"duplicate group name in database: {name!r}")

        desired[name] = GroupConfig(
            index=row["id"],
            name=name,
            wecom_robot_id=str(row["wecom_robot_id"]),
            wecom_robot_secret=str(row["wecom_robot_secret"]),
            dify_api_key=str(row["dify_api_key"]),
            session_max_total=(
                row.get("session_max_total") or defaults.session_max_total
            ),
            session_ttl_seconds=row.get("session_ttl_seconds"),
        )
    return desired


@dataclass
class GroupDiff:
    """What the reconcile loop must do to converge on the desired state."""

    added: List[GroupConfig] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    restarted: List[GroupConfig] = field(default_factory=list)
    updated: List[GroupConfig] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.restarted or self.updated)


def compute_group_diff(
    live: Sequence[str],
    current: Mapping[str, GroupConfig],
    desired: Mapping[str, GroupConfig],
) -> GroupDiff:
    """Diff the desired state against what is actually running.

    ``live`` are the names of clients that are (or were just started as)
    connected — not the last-known configuration — so a group whose connect
    failed is treated as missing and retried on the next cycle. ``current``
    (usually the :class:`~src.config_store.GroupStore` snapshot) holds the
    last-applied configuration used to detect field changes.

    Credential changes (robot id/secret) require a client restart; every other
    change is applied in place without touching the connection.
    """
    diff = GroupDiff()
    live_names = set(live)

    for name in live_names:
        if name not in desired:
            diff.removed.append(name)

    for name, group in desired.items():
        if name not in live_names:
            diff.added.append(group)
            continue
        previous = current.get(name)
        if (
            previous is None
            or previous.wecom_robot_id != group.wecom_robot_id
            or previous.wecom_robot_secret != group.wecom_robot_secret
        ):
            diff.restarted.append(group)
        elif previous != group:
            diff.updated.append(group)

    return diff


class GroupRepository:
    """asyncpg-backed access to the ``groups`` table. Plain SQL, no ORM."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Open the connection pool and ensure the schema exists."""
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        assert self._pool is not None
        await self._pool.execute(DDL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _pool_or_raise(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("GroupRepository is not connected")
        return self._pool

    async def list_rows(self) -> List[Mapping[str, Any]]:
        """All rows, disabled ones included (diffing skips them)."""
        return list(await self._pool_or_raise().fetch(
            f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY id"
        ))

    async def insert_group(
        self,
        *,
        name: str,
        wecom_robot_id: str,
        wecom_robot_secret: str,
        dify_api_key: str,
        session_max_total: Optional[int] = None,
        session_ttl_seconds: Optional[int] = None,
        enabled: bool = True,
    ) -> int:
        """Insert one group; returns its new id."""
        pool = self._pool_or_raise()
        row = await pool.fetchrow(
            """
            INSERT INTO groups (
                name, wecom_robot_id, wecom_robot_secret, dify_api_key,
                session_max_total, session_ttl_seconds, enabled
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            name,
            wecom_robot_id,
            wecom_robot_secret,
            dify_api_key,
            session_max_total,
            session_ttl_seconds,
            enabled,
        )
        assert row is not None
        return row["id"]

    async def update_group(
        self,
        group_id: int,
        *,
        name: Optional[str] = None,
        wecom_robot_id: Optional[str] = None,
        wecom_robot_secret: Optional[str] = None,
        dify_api_key: Optional[str] = None,
        session_max_total: Any = _UNSET,
        session_ttl_seconds: Any = _UNSET,
        enabled: Optional[bool] = None,
    ) -> bool:
        """Update provided fields; returns False when the row does not exist.

        ``None`` on a credential field means "keep"; ``_UNSET`` on a session
        field means "keep" while an explicit ``None`` means "clear the column,
        inherit the global default again".
        """
        assignments: List[str] = ["updated_at = now()"]
        params: List[Any] = [group_id]

        def _set(column: str, value: Any) -> None:
            params.append(value)
            assignments.append(f"{column} = ${len(params)}")

        if name is not None:
            _set("name", name)
        if wecom_robot_id is not None:
            _set("wecom_robot_id", wecom_robot_id)
        if wecom_robot_secret is not None:
            _set("wecom_robot_secret", wecom_robot_secret)
        if dify_api_key is not None:
            _set("dify_api_key", dify_api_key)
        if session_max_total is not _UNSET:
            _set("session_max_total", session_max_total)
        if session_ttl_seconds is not _UNSET:
            _set("session_ttl_seconds", session_ttl_seconds)
        if enabled is not None:
            _set("enabled", enabled)

        pool = self._pool_or_raise()
        row = await pool.fetchrow(
            f"UPDATE groups SET {', '.join(assignments)} WHERE id = $1 RETURNING id",
            *params,
        )
        return row is not None

    async def delete_group(self, group_id: int) -> bool:
        """Delete one group; returns False when the row does not exist."""
        pool = self._pool_or_raise()
        row = await pool.fetchrow(
            "DELETE FROM groups WHERE id = $1 RETURNING id", group_id
        )
        return row is not None

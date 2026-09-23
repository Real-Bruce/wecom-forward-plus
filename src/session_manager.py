"""Per-group session pools with TTL expiry, LRU cap eviction, and reset.

Each configured group owns an independent :class:`GroupPool` mapping a
``wx_user`` (Dify user id) to a :class:`Session`. Expired sessions are removed
lazily on access and optionally by a background sweeper thread started from
:meth:`SessionManager.start_sweeper`. All public methods are guarded by a
re-entrant lock so the background thread and the asyncio loop can share them.

Pools are created for the initial group set at construction time; when groups
are database-managed, the reconcile loop mutates the set at runtime through
:meth:`ensure_pool` / :meth:`update_pool` / :meth:`remove_pool`. Parameter
updates are applied in place so live conversations survive them; removing a
group drops its pool and every session in it.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from .config import Config, GroupConfig


@dataclass
class Session:
    """A single user's Dify conversation within one group.

    ``conversation_id`` is ``None`` until Dify returns one for the first
    message of a conversation. ``last_active_at`` uses a monotonic clock and
    drives TTL expiry; ``created_at`` is wall-clock time for diagnostics.
    """

    conversation_id: str
    last_active_at: float
    created_at: float


class GroupPool:
    """Sessions for a single group, kept in least-recently-used order."""

    def __init__(
        self,
        ttl_seconds: float,
        max_total: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_total <= 0:
            raise ValueError("max_total must be at least 1")
        self._ttl_seconds = ttl_seconds
        self._max_total = max_total
        self._clock = clock
        # Front of the OrderedDict is least-recently-used.
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.RLock()

    def get_or_create(self, wx_user: str) -> Session:
        with self._lock:
            self._evict_expired_locked()

            session = self._sessions.get(wx_user)
            if session is not None:
                session.last_active_at = self._clock()
                self._sessions.move_to_end(wx_user)
                return session

            if len(self._sessions) >= self._max_total:
                # Evict the least-recently-used entry to stay under the cap.
                self._sessions.popitem(last=False)

            now = self._clock()
            session = Session(
                conversation_id=None,
                last_active_at=now,
                created_at=time.time(),
            )
            self._sessions[wx_user] = session
            return session

    def reset(self, wx_user: str) -> None:
        with self._lock:
            self._sessions.pop(wx_user, None)

    def sweep_expired(self) -> None:
        with self._lock:
            self._evict_expired_locked()

    def set_ttl_seconds(self, ttl_seconds: float) -> None:
        """Apply a new TTL; sessions keep their positions and history."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with self._lock:
            self._ttl_seconds = ttl_seconds

    def set_max_total(self, max_total: int) -> None:
        """Apply a new cap, evicting LRU sessions immediately when it shrinks."""
        if max_total <= 0:
            raise ValueError("max_total must be at least 1")
        with self._lock:
            self._max_total = max_total
            while len(self._sessions) > self._max_total:
                self._sessions.popitem(last=False)

    def _evict_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            user
            for user, session in self._sessions.items()
            if now - session.last_active_at > self._ttl_seconds
        ]
        for user in expired:
            self._sessions.pop(user, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __contains__(self, wx_user: str) -> bool:
        with self._lock:
            return wx_user in self._sessions


class SessionManager:
    """Owns one :class:`GroupPool` per configured group."""

    def __init__(
        self,
        config: Config,
        groups: Optional[Sequence[GroupConfig]] = None,
    ) -> None:
        """Build a pool per group.

        ``groups`` defaults to ``config.groups``; database mode passes the
        groups currently loaded from the database instead. Each pool resolves
        its TTL as the group's override or the global default.
        """
        if groups is None:
            groups = config.groups
        self._pools: Dict[str, GroupPool] = {}
        self._lock = threading.RLock()
        for group in groups:
            self._pools[group.name] = GroupPool(
                ttl_seconds=group.session_ttl_seconds or config.session_ttl_seconds,
                max_total=group.session_max_total,
            )

    def _pool(self, group_id: str) -> GroupPool:
        with self._lock:
            pool = self._pools.get(group_id)
        if pool is None:
            raise KeyError(f"Unknown group: {group_id}")
        return pool

    def ensure_pool(self, group: GroupConfig, default_ttl: int) -> None:
        """Create the group's pool if absent (no-op when it already exists)."""
        with self._lock:
            if group.name in self._pools:
                return
            self._pools[group.name] = GroupPool(
                ttl_seconds=group.session_ttl_seconds or default_ttl,
                max_total=group.session_max_total,
            )

    def update_pool(self, group: GroupConfig, default_ttl: int) -> None:
        """Apply new session parameters in place; sessions are kept."""
        pool = self._pool(group.name)
        pool.set_ttl_seconds(group.session_ttl_seconds or default_ttl)
        pool.set_max_total(group.session_max_total)

    def remove_pool(self, group_id: str) -> None:
        """Drop the group's pool together with all its sessions."""
        with self._lock:
            self._pools.pop(group_id, None)

    def get_or_create(self, group_id: str, wx_user: str) -> Session:
        return self._pool(group_id).get_or_create(wx_user)

    def reset(self, group_id: str, wx_user: str) -> None:
        self._pool(group_id).reset(wx_user)

    def sweep_expired(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
        for pool in pools:
            pool.sweep_expired()

    def size(self) -> int:
        with self._lock:
            pools = list(self._pools.values())
        return sum(len(pool) for pool in pools)

    def start_sweeper(
        self, interval: float = 60.0
    ) -> Tuple[threading.Thread, threading.Event]:
        """Start a daemon thread that periodically drops expired sessions.

        Returns the thread and its stop event so callers can stop it.
        """

        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                stop.wait(interval)
                if not stop.is_set():
                    self.sweep_expired()

        thread = threading.Thread(target=_loop, name="session-sweeper", daemon=True)
        thread.start()
        return thread, stop
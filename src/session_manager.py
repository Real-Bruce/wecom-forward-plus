"""Per-group session pools with TTL expiry, LRU cap eviction, and reset.

Each configured group owns an independent :class:`GroupPool` mapping a
``wx_user`` (Dify user id) to a :class:`Session`. Expired sessions are removed
lazily on access and optionally by a background sweeper thread started from
:meth:`SessionManager.start_sweeper`. All public methods are guarded by a
re-entrant lock so the background thread and the asyncio loop can share them.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from .config import Config


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

    def __init__(self, config: Config) -> None:
        self._pools: Dict[str, GroupPool] = {}
        for group in config.groups:
            self._pools[group.name] = GroupPool(
                ttl_seconds=config.session_ttl_seconds,
                max_total=group.session_max_total,
            )

    def _pool(self, group_id: str) -> GroupPool:
        pool = self._pools.get(group_id)
        if pool is None:
            raise KeyError(f"Unknown group: {group_id}")
        return pool

    def get_or_create(self, group_id: str, wx_user: str) -> Session:
        return self._pool(group_id).get_or_create(wx_user)

    def reset(self, group_id: str, wx_user: str) -> None:
        self._pool(group_id).reset(wx_user)

    def sweep_expired(self) -> None:
        for pool in self._pools.values():
            pool.sweep_expired()

    def size(self) -> int:
        return sum(len(pool) for pool in self._pools.values())

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
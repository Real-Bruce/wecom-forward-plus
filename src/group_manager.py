"""Live WeCom client lifecycle and the database reconcile loop.

:class:`GroupManager` owns the running :class:`~src.wecom_client.WeComClient`
per group and converges on a desired state produced from database rows:

- added group (new, re-enabled, or a previous connect failure) -> start client
- removed group -> disconnect client, drop its session pool
- credentials changed -> restart the client, keep the session pool
- other fields changed (Dify API key, session parameters) -> apply in place

:func:`reconcile_loop` periodically re-reads the database and applies the
diff. Any failure (database unreachable, invalid rows) is logged and the
last-known configuration keeps running; the next cycle retries. Setting
``reload_event`` wakes the loop immediately so admin-UI writes take effect
within one database round-trip instead of waiting out the interval.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, Mapping, Sequence, Set

from .config import GroupConfig
from .config_store import GroupStore
from .db_config import GroupDefaults, GroupRepository, compute_group_diff, rows_to_desired
from .message_handler import MessageHandler
from .session_manager import SessionManager
from .wecom_client import WeComClient

logger = logging.getLogger(__name__)


class GroupManager:
    """Owns the live WeCom client per group and applies desired state."""

    def __init__(
        self,
        *,
        handler: MessageHandler,
        sessions: SessionManager,
        store: GroupStore,
        global_ttl: int,
        client_factory: Callable[..., WeComClient] = WeComClient,
    ) -> None:
        self._handler = handler
        self._sessions = sessions
        self._store = store
        self._global_ttl = global_ttl
        self._client_factory = client_factory
        self._clients: Dict[str, WeComClient] = {}
        self._connect_tasks: Dict[str, asyncio.Task] = {}

    @property
    def live_group_names(self) -> Set[str]:
        """Names of clients that exist (connected or connecting)."""
        return set(self._clients)

    async def apply(self, desired: Mapping[str, GroupConfig]) -> None:
        """Converge on ``desired``; never raises for per-group failures."""
        diff = compute_group_diff(
            self.live_group_names, self._store.snapshot(), desired
        )

        for name in diff.removed:
            client = self._clients.pop(name, None)
            if client is not None:
                self._disconnect(name, client)
            self._sessions.remove_pool(name)
            self._store.remove(name)
            logger.info("Stopped WeCom client for group=%s", name)

        for group in diff.restarted:
            old = self._clients.pop(group.name, None)
            if old is not None:
                self._disconnect(group.name, old)
            # Keep the session pool: users keep their conversation ids.
            self._sessions.ensure_pool(group, self._global_ttl)
            self._store.put(group)
            self._start(group)
            logger.info(
                "Restarted WeCom client for group=%s (credentials changed)",
                group.name,
            )

        for group in diff.updated:
            self._store.put(group)
            self._sessions.update_pool(group, self._global_ttl)
            logger.info(
                "Updated configuration for group=%s without reconnect", group.name
            )

        for group in diff.added:
            self._store.put(group)
            self._sessions.ensure_pool(group, self._global_ttl)
            self._start(group)
            logger.info("Started WeCom client for group=%s", group.name)

    async def shutdown(self) -> None:
        """Cancel pending connects and disconnect every client."""
        tasks = list(self._connect_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connect_tasks.clear()

        for name, client in list(self._clients.items()):
            self._disconnect(name, client)
        self._clients.clear()

    def _start(self, group: GroupConfig) -> None:
        client = self._client_factory(
            group_id=group.name,
            robot_id=group.wecom_robot_id,
            robot_secret=group.wecom_robot_secret,
            handler=self._handler.handler_for(group.name),
        )
        self._clients[group.name] = client

        # Connect as a detached task: a failing group must not block the
        # reconcile loop (or the whole startup).
        task = asyncio.create_task(client.connect(), name=f"wecom-connect-{group.name}")
        self._connect_tasks[group.name] = task

        def _done(task: asyncio.Task) -> None:
            self._connect_tasks.pop(group.name, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Connect failed for group=%s; retrying next cycle",
                    group.name,
                )
                # Drop the dead client so the next apply() re-adds it.
                if self._clients.get(group.name) is client:
                    self._clients.pop(group.name, None)
            else:
                logger.info("WeCom client connected for group=%s", group.name)

        task.add_done_callback(_done)

    def _disconnect(self, name: str, client: WeComClient) -> None:
        task = self._connect_tasks.pop(name, None)
        if task is not None:
            task.cancel()
        try:
            client.disconnect()
        except Exception:
            logger.warning(
                "Failed to disconnect client for group=%s", name, exc_info=True
            )


async def reconcile_loop(
    repo: GroupRepository,
    manager: GroupManager,
    defaults: GroupDefaults,
    interval: float,
    stop_event: asyncio.Event,
    reload_event: asyncio.Event,
) -> None:
    """Periodically apply the database's group configuration.

    Runs one reconcile immediately, then waits until the interval elapses,
    ``reload_event`` is set (an admin write), or ``stop_event`` is set. The
    reload event is cleared *after* waiting, so a write that lands while
    ``apply`` is still running is honored on the very next iteration.
    """
    while not stop_event.is_set():
        try:
            desired = rows_to_desired(await repo.list_rows(), defaults)
            await manager.apply(desired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Group config reload failed; keeping last-known config",
                exc_info=True,
            )

        stop_waiter = asyncio.ensure_future(stop_event.wait())
        reload_waiter = asyncio.ensure_future(reload_event.wait())
        try:
            await asyncio.wait(
                {stop_waiter, reload_waiter},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in (stop_waiter, reload_waiter):
                if not waiter.done():
                    waiter.cancel()
        reload_event.clear()

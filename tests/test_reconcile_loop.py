"""Tests for the reconcile loop with a fake repository and manager."""

import asyncio

import pytest

from src.db_config import GroupDefaults
from src.group_manager import reconcile_loop

DEFAULTS = GroupDefaults(session_ttl_seconds=300, session_max_total=200)

_ROW = {
    "id": 1,
    "name": "sales",
    "wecom_robot_id": "bot-1",
    "wecom_robot_secret": "sec-1",
    "dify_api_key": "app-1",
    "session_max_total": None,
    "session_ttl_seconds": None,
    "enabled": True,
}


class FakeRepo:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows) if rows is not None else [dict(_ROW)]
        self.error = error
        self.list_calls = 0

    async def list_rows(self):
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


class FakeManager:
    def __init__(self):
        self.applied = []  # list of desired mappings

    async def apply(self, desired):
        self.applied.append(desired)

    async def shutdown(self):
        pass


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _start_loop(repo, manager, interval=60.0):
    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    task = asyncio.create_task(
        reconcile_loop(repo, manager, DEFAULTS, interval, stop_event, reload_event)
    )
    return task, stop_event, reload_event


async def test_loop_applies_desired_state_and_stops():
    repo = FakeRepo()
    manager = FakeManager()
    task, stop_event, _ = _start_loop(repo, manager)

    await _wait_until(lambda: len(manager.applied) >= 1)
    assert manager.applied[0]["sales"].name == "sales"

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)  # exits promptly, no exception


async def test_repo_failure_keeps_last_known_and_survives():
    repo = FakeRepo(error=RuntimeError("db unreachable"))
    manager = FakeManager()
    task, stop_event, _ = _start_loop(repo, manager, interval=0.01)

    # The loop keeps running (retrying) despite the persistent failure…
    await _wait_until(lambda: repo.list_calls >= 3)
    assert manager.applied == []  # …and never applied anything.

    # Recovery: the next cycle applies the now-available rows.
    repo.error = None
    await _wait_until(lambda: len(manager.applied) >= 1)

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)


async def test_invalid_rows_skip_cycle():
    bad_row = dict(_ROW, name="", id=2)
    repo = FakeRepo(rows=[bad_row])
    manager = FakeManager()
    task, stop_event, _ = _start_loop(repo, manager, interval=0.01)

    await _wait_until(lambda: repo.list_calls >= 2)
    assert manager.applied == []  # invalid rows never reach the manager
    assert not task.done()  # loop survived the validation error

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)


async def test_reload_event_wakes_loop_immediately():
    repo = FakeRepo()
    manager = FakeManager()
    task, _, reload_event = _start_loop(repo, manager, interval=60.0)

    await _wait_until(lambda: len(manager.applied) == 1)
    # A write pokes the event: the second apply happens well before the
    # 60s interval elapses.
    reload_event.set()
    await _wait_until(lambda: len(manager.applied) == 2, timeout=2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

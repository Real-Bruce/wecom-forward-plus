"""Tests for GroupManager's apply/shutdown lifecycle with injected fakes."""

import asyncio

from src.config import Config, GroupConfig
from src.config_store import GroupStore
from src.group_manager import GroupManager
from src.session_manager import SessionManager


class StubHandler:
    """Just enough of MessageHandler for client factory wiring."""

    def handler_for(self, group_id):
        async def _handle(from_user, content, attachments=()):
            return "ok"

        return _handle


class FakeClient:
    """Records connect/disconnect; can be made to fail connecting."""

    def __init__(self, connect_error=None):
        self.connect_error = connect_error
        self.connect_called = False
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connect_called = True
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def disconnect(self):
        self.disconnected = True


class ClientFactory:
    """Builds FakeClients and records the kwargs each one was created with."""

    def __init__(self, connect_error=None):
        self.connect_error = connect_error
        self.created = []

    def __call__(self, **kwargs):
        self.created.append(kwargs)
        return FakeClient(connect_error=self.connect_error)


def _group(name="sales", **overrides):
    group = GroupConfig(
        index=1,
        name=name,
        wecom_robot_id="bot-1",
        wecom_robot_secret="sec-1",
        dify_api_key="app-1",
        session_max_total=200,
    )
    for key, value in overrides.items():
        setattr(group, key, value)
    return group


def _manager(groups=(), connect_error=None):
    config = Config(
        dify_base_url="https://dify.example.com/v1",
        session_ttl_seconds=300,
        session_max_total=200,
        reset_keywords=[],
        groups=[],
    )
    sessions = SessionManager(config, list(groups))
    store = GroupStore({group.name: group for group in groups})
    factory = ClientFactory(connect_error=connect_error)
    manager = GroupManager(
        handler=StubHandler(),
        sessions=sessions,
        store=store,
        global_ttl=300,
        client_factory=factory,
    )
    return manager, sessions, store, factory


async def _let_callbacks_run():
    # Two ticks: one for the detached connect task, one for its done-callback.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_apply_starts_clients_for_new_groups():
    manager, _, _, factory = _manager(groups=[_group("a"), _group("b")])

    await manager.apply({"a": _group("a"), "b": _group("b")})
    await _let_callbacks_run()

    assert manager.live_group_names == {"a", "b"}
    assert [c["group_id"] for c in factory.created] == ["a", "b"]
    assert all(client.connected for client in manager._clients.values())


async def test_apply_removes_clients_and_pools_for_removed_groups():
    group = _group("a")
    manager, sessions, _, _ = _manager(groups=[group])
    await manager.apply({"a": group})
    await _let_callbacks_run()
    client = manager._clients["a"]
    sessions.get_or_create("a", "wx_user")  # a live session exists

    await manager.apply({})  # group deleted in the database
    await _let_callbacks_run()

    assert manager.live_group_names == set()
    assert client.disconnected
    assert sessions.size() == 0  # pool dropped with its sessions


async def test_apply_restarts_on_credential_change_and_keeps_pool():
    group = _group("a")
    manager, sessions, store, factory = _manager(groups=[group])
    await manager.apply({"a": group})
    await _let_callbacks_run()
    session = sessions.get_or_create("a", "wx_user")
    session.conversation_id = "conv-1"
    first_client = manager._clients["a"]

    rotated = _group("a", wecom_robot_secret="sec-2")
    await manager.apply({"a": rotated})
    await _let_callbacks_run()

    assert manager.live_group_names == {"a"}
    assert first_client.disconnected
    assert manager._clients["a"] is not first_client
    assert store.get("a").wecom_robot_secret == "sec-2"
    # Session pool kept: the conversation survives a credential rotation.
    assert sessions.get_or_create("a", "wx_user").conversation_id == "conv-1"


async def test_apply_dify_key_change_updates_store_without_restart():
    group = _group("a")
    manager, _, store, _ = _manager(groups=[group])
    await manager.apply({"a": group})
    await _let_callbacks_run()
    first_client = manager._clients["a"]

    rotated = _group("a", dify_api_key="app-2")
    await manager.apply({"a": rotated})
    await _let_callbacks_run()

    assert manager._clients["a"] is first_client
    assert not first_client.disconnected
    assert store.get("a").dify_api_key == "app-2"


async def test_apply_session_param_change_updates_pool_in_place():
    group = _group("a")
    manager, sessions, _, _ = _manager(groups=[group])
    await manager.apply({"a": group})
    await _let_callbacks_run()
    sessions.get_or_create("a", "wx_user")

    tuned = _group("a", session_max_total=5, session_ttl_seconds=10)
    await manager.apply({"a": tuned})
    await _let_callbacks_run()

    assert sessions.size() == 1  # session survived the param update
    assert sessions._pools["a"]._max_total == 5


async def test_apply_is_idempotent():
    group = _group("a")
    manager, _, _, factory = _manager(groups=[group])
    desired = {"a": group}

    await manager.apply(desired)
    await _let_callbacks_run()
    await manager.apply(desired)  # second identical apply
    await _let_callbacks_run()

    assert len(factory.created) == 1
    assert manager.live_group_names == {"a"}


async def test_connect_failure_removes_client_for_retry():
    group = _group("a")
    manager, sessions, store, _ = _manager(
        groups=[group], connect_error=RuntimeError("connect boom")
    )

    await manager.apply({"a": group})
    await _let_callbacks_run()
    assert manager.live_group_names == set()  # failed connect dropped it
    assert store.get("a") is group  # config kept for the retry
    assert sessions.size() == 0

    # Next cycle retries the connect.
    manager._client_factory = ClientFactory()
    await manager.apply({"a": group})
    await _let_callbacks_run()
    assert manager.live_group_names == {"a"}


async def test_shutdown_disconnects_everything():
    group = _group("a")
    manager, _, _, _ = _manager(groups=[group])
    await manager.apply({"a": group})
    await _let_callbacks_run()
    client = manager._clients["a"]

    await manager.shutdown()

    assert manager.live_group_names == set()
    assert client.disconnected
    assert manager._connect_tasks == {}


async def test_shutdown_with_pending_connect_cancels_it():
    manager, _, _, _ = _manager(groups=[_group("a")])

    await manager.apply({"a": _group("a")})
    await manager.shutdown()  # connect task still pending
    await _let_callbacks_run()

    assert manager.live_group_names == set()
    assert manager._connect_tasks == {}

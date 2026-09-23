"""Tests for per-group session pools (TTL, cap eviction, reset)."""

import pytest

from src.config import Config, GroupConfig
from src.session_manager import GroupPool, SessionManager


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config(refetch_slot=None, max_total=200, ttl=300):
    groups = [
        GroupConfig(
            index=1,
            name="group-1",
            wecom_robot_id="bot-1",
            wecom_robot_secret="sec-1",
            dify_api_key="app-1",
            session_max_total=max_total,
        )
    ]
    return Config(
        dify_base_url="https://dify.example.com/v1",
        session_ttl_seconds=ttl,
        session_max_total=max_total,
        reset_keywords=["重置"],
        groups=groups,
    )


def test_get_or_create_returns_same_session():
    clock = FakeClock()
    pool = GroupPool(ttl_seconds=300, max_total=10, clock=clock)
    first = pool.get_or_create("wx_alice")
    assert first.conversation_id is None

    first.conversation_id = "conv-1"
    clock.advance(50)
    second = pool.get_or_create("wx_alice")
    assert second is first
    assert second.conversation_id == "conv-1"
    assert len(pool) == 1


def test_ttl_expiration_creates_new_session():
    clock = FakeClock()
    pool = GroupPool(ttl_seconds=300, max_total=10, clock=clock)
    pool.get_or_create("wx_alice")

    clock.advance(301)  # just past the TTL
    fresh = pool.get_or_create("wx_alice")
    assert fresh.conversation_id is None
    assert len(pool) == 1


def test_cap_evicts_least_recently_used():
    pool = GroupPool(ttl_seconds=300, max_total=2, clock=FakeClock())
    pool.get_or_create("wx_a")
    pool.get_or_create("wx_b")
    # "wx_a" is LRU; adding a third user evicts it.
    pool.get_or_create("wx_c")

    assert len(pool) == 2
    assert "wx_a" not in pool
    assert "wx_b" in pool
    assert "wx_c" in pool


def test_access_refreshes_lru_order():
    pool = GroupPool(ttl_seconds=300, max_total=2, clock=FakeClock())
    pool.get_or_create("wx_a")
    pool.get_or_create("wx_b")
    # Touch wx_a so it is no longer LRU, then evict triggers on wx_b.
    pool.get_or_create("wx_a")
    pool.get_or_create("wx_c")

    assert "wx_a" in pool
    assert "wx_b" not in pool
    assert "wx_c" in pool


def test_reset_removes_session():
    pool = GroupPool(ttl_seconds=300, max_total=10, clock=FakeClock())
    pool.get_or_create("wx_alice")
    pool.reset("wx_alice")
    assert "wx_alice" not in pool
    assert len(pool) == 0


def test_sweep_removes_only_expired():
    clock = FakeClock()
    pool = GroupPool(ttl_seconds=300, max_total=10, clock=clock)
    pool.get_or_create("wx_a")
    clock.advance(200)
    pool.get_or_create("wx_b")
    clock.advance(150)  # wx_a is now 350s old, wx_b is 150s old

    pool.sweep_expired()
    assert "wx_a" not in pool
    assert "wx_b" in pool


def test_session_manager_isolates_groups():
    groups = [
        GroupConfig(1, "group-a", "b1", "s1", "app-1", 200),
        GroupConfig(2, "group-b", "b2", "s2", "app-2", 200),
    ]
    config = Config(
        dify_base_url="https://dify.example.com/v1",
        session_ttl_seconds=300,
        session_max_total=200,
        reset_keywords=["重置"],
        groups=groups,
    )
    manager = SessionManager(config)

    # Same user id is independent across groups.
    a = manager.get_or_create("group-a", "wx_alice")
    a.conversation_id = "conv-a"
    b = manager.get_or_create("group-b", "wx_alice")
    assert b.conversation_id is None
    assert manager.get_or_create("group-a", "wx_alice").conversation_id == "conv-a"

    manager.reset("group-b", "wx_alice")
    assert manager.size() == 1  # only group-a's session survives


# -- runtime mutation (database mode) ------------------------------------------


def _group(name, ttl=None, max_total=200):
    return GroupConfig(
        index=1,
        name=name,
        wecom_robot_id="bot-1",
        wecom_robot_secret="sec-1",
        dify_api_key="app-1",
        session_max_total=max_total,
        session_ttl_seconds=ttl,
    )


def test_explicit_groups_and_ttl_override():
    config = _config(ttl=300)
    groups = [_group("g1", ttl=60), _group("g2")]
    manager = SessionManager(config, groups)

    clock = FakeClock()
    manager._pools["g1"]._clock = clock  # direct access for the TTL assertion

    manager.get_or_create("g1", "wx_a")
    clock.advance(61)  # past the group override (60), before global (300)
    manager.sweep_expired()
    assert manager.size() == 0


def test_ensure_pool_is_noop_when_present():
    config = _config()
    manager = SessionManager(config)
    manager.ensure_pool(config.groups[0], config.session_ttl_seconds)
    manager.get_or_create("group-1", "wx_a")

    # Re-ensuring must not recreate the pool (sessions survive).
    manager.ensure_pool(config.groups[0], config.session_ttl_seconds)
    assert manager.size() == 1


def test_ensure_pool_creates_with_group_ttl_override():
    config = _config(ttl=300)
    manager = SessionManager(config, groups=[])
    manager.ensure_pool(_group("g1", ttl=60), config.session_ttl_seconds)

    clock = FakeClock()
    manager._pools["g1"]._clock = clock
    manager.get_or_create("g1", "wx_a")
    clock.advance(61)
    manager.sweep_expired()
    assert manager.size() == 0


def test_update_pool_changes_ttl_and_max_in_place():
    config = _config()
    manager = SessionManager(config)
    clock = FakeClock()
    manager._pools["group-1"]._clock = clock

    manager.get_or_create("group-1", "wx_a")
    manager.get_or_create("group-1", "wx_b")
    session = manager.get_or_create("group-1", "wx_a")

    manager.update_pool(
        _group("group-1", ttl=10, max_total=1), config.session_ttl_seconds
    )

    # Same session object survived (in-place, not recreated).
    assert manager.get_or_create("group-1", "wx_a") is session
    # Cap shrink evicted the LRU entry (wx_b).
    assert "wx_b" not in manager._pools["group-1"]

    # New TTL applies: 11s idle expires the session.
    clock.advance(11)
    manager.sweep_expired()
    assert manager.size() == 0


def test_update_pool_unknown_group_raises():
    config = _config()
    manager = SessionManager(config)
    with pytest.raises(KeyError):
        manager.update_pool(_group("missing"), config.session_ttl_seconds)


def test_remove_pool_drops_sessions():
    config = _config()
    manager = SessionManager(config)
    manager.get_or_create("group-1", "wx_a")
    assert manager.size() == 1

    manager.remove_pool("group-1")
    assert manager.size() == 0
    with pytest.raises(KeyError):
        manager.get_or_create("group-1", "wx_a")
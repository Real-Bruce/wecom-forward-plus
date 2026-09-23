"""Tests for the runtime GroupStore registry."""

import pytest

from src.config import GroupConfig
from src.config_store import GroupStore


def _group(name="group-1", dify_api_key="app-1"):
    return GroupConfig(
        index=1,
        name=name,
        wecom_robot_id="bot-1",
        wecom_robot_secret="sec-1",
        dify_api_key=dify_api_key,
        session_max_total=200,
    )


def test_put_get_remove():
    store = GroupStore()
    assert len(store) == 0

    store.put(_group())
    assert store.get("group-1").dify_api_key == "app-1"
    assert len(store) == 1

    store.remove("group-1")
    assert len(store) == 0
    store.remove("group-1")  # removing again is a no-op


def test_get_unknown_raises_keyerror():
    store = GroupStore()
    with pytest.raises(KeyError, match="Unknown group"):
        store.get("nope")


def test_snapshot_is_copy():
    store = GroupStore({"group-1": _group()})
    snapshot = store.snapshot()
    assert set(snapshot) == {"group-1"}

    store.remove("group-1")
    assert set(snapshot) == {"group-1"}  # snapshot unaffected
    assert len(store) == 0


def test_from_config():
    from src.config import Config

    config = Config(
        dify_base_url="https://dify.example.com/v1",
        session_ttl_seconds=300,
        session_max_total=200,
        reset_keywords=["重置"],
        groups=[_group("a"), _group("b", dify_api_key="app-2")],
    )
    store = GroupStore.from_config(config)
    assert len(store) == 2
    assert store.get("b").dify_api_key == "app-2"

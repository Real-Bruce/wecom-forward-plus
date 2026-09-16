"""Tests for environment-based configuration parsing."""

import pytest

from src.config import (
    DEFAULT_RESET_KEYWORDS,
    DEFAULT_SESSION_MAX_TOTAL,
    DEFAULT_SESSION_TTL_SECONDS,
    ConfigError,
    load_config,
)

BASE = {
    "WECOM_FORWARD_PLUS_DIFY_BASE_URL": "https://dify.example.com/v1",
    "WECOM_FORWARD_PLUS_GROUP_1_WECOM_ROBOT_ID": "bot-1",
    "WECOM_FORWARD_PLUS_GROUP_1_WECOM_ROBOT_SECRET": "sec-1",
    "WECOM_FORWARD_PLUS_GROUP_1_DIFY_API_KEY": "app-1",
}


def _env(**overrides):
    env = dict(BASE)
    env.update(overrides)
    return env


def test_minimal_config_defaults():
    config = load_config(_env())
    assert config.dify_base_url == "https://dify.example.com/v1"
    assert config.session_ttl_seconds == DEFAULT_SESSION_TTL_SECONDS
    assert config.session_max_total == DEFAULT_SESSION_MAX_TOTAL
    assert config.reset_keywords == DEFAULT_RESET_KEYWORDS
    assert len(config.groups) == 1

    group = config.groups[0]
    assert group.index == 1
    assert group.name == "group-1"
    assert group.wecom_robot_id == "bot-1"
    assert group.wecom_robot_secret == "sec-1"
    assert group.dify_api_key == "app-1"
    assert group.session_max_total == DEFAULT_SESSION_MAX_TOTAL


def test_multiple_groups_and_overrides():
    env = _env(
        **{
            "WECOM_FORWARD_PLUS_GROUP_1_NAME": "sales",
            "WECOM_FORWARD_PLUS_GROUP_1_SESSION_MAX_TOTAL": "300",
            "WECOM_FORWARD_PLUS_GROUP_2_NAME": "support",
            "WECOM_FORWARD_PLUS_GROUP_2_WECOM_ROBOT_ID": "bot-2",
            "WECOM_FORWARD_PLUS_GROUP_2_WECOM_ROBOT_SECRET": "sec-2",
            "WECOM_FORWARD_PLUS_GROUP_2_DIFY_API_KEY": "app-2",
        }
    )
    config = load_config(env)
    assert [g.name for g in config.groups] == ["sales", "support"]
    assert config.groups[0].session_max_total == 300
    assert config.groups[1].session_max_total == DEFAULT_SESSION_MAX_TOTAL
    assert config.get_group("support").dify_api_key == "app-2"


def test_comma_separated_reset_keywords():
    config = load_config(
        _env(**{"WECOM_FORWARD_PLUS_SESSION_RESET_KEYWORDS": "重置,新对话"})
    )
    assert config.reset_keywords == ["重置", "新对话"]


def test_json_reset_keywords():
    config = load_config(
        _env(**{"WECOM_FORWARD_PLUS_SESSION_RESET_KEYWORDS": '["a", "b"]'})
    )
    assert config.reset_keywords == ["a", "b"]


def test_missing_base_url():
    env = _env()
    del env["WECOM_FORWARD_PLUS_DIFY_BASE_URL"]
    with pytest.raises(ConfigError, match="DIFY_BASE_URL is required"):
        load_config(env)


def test_invalid_base_url_scheme():
    with pytest.raises(ConfigError, match="http:// or https://"):
        load_config(_env(**{"WECOM_FORWARD_PLUS_DIFY_BASE_URL": "ftp://x/v1"}))


def test_incomplete_group():
    env = _env()
    del env["WECOM_FORWARD_PLUS_GROUP_1_DIFY_API_KEY"]
    with pytest.raises(ConfigError, match="incomplete"):
        load_config(env)


def test_gapped_group_indices():
    env = _env(
        **{
            "WECOM_FORWARD_PLUS_GROUP_3_WECOM_ROBOT_ID": "bot-3",
            "WECOM_FORWARD_PLUS_GROUP_3_WECOM_ROBOT_SECRET": "sec-3",
            "WECOM_FORWARD_PLUS_GROUP_3_DIFY_API_KEY": "app-3",
        }
    )
    with pytest.raises(ConfigError, match="contiguous"):
        load_config(env)


def test_no_groups():
    env = {
        "WECOM_FORWARD_PLUS_DIFY_BASE_URL": "https://dify.example.com/v1",
    }
    with pytest.raises(ConfigError, match="No groups configured"):
        load_config(env)


def test_duplicate_group_names():
    env = _env(
        **{
            "WECOM_FORWARD_PLUS_GROUP_2_NAME": "group-1",
            "WECOM_FORWARD_PLUS_GROUP_2_WECOM_ROBOT_ID": "bot-2",
            "WECOM_FORWARD_PLUS_GROUP_2_WECOM_ROBOT_SECRET": "sec-2",
            "WECOM_FORWARD_PLUS_GROUP_2_DIFY_API_KEY": "app-2",
        }
    )
    with pytest.raises(ConfigError, match="Duplicate group name"):
        load_config(env)


def test_invalid_integer():
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(_env(**{"WECOM_FORWARD_PLUS_SESSION_TTL_SECONDS": "abc"}))
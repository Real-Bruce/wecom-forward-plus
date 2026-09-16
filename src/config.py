"""Load and validate configuration from environment variables.

All configuration is read from environment variables with the
``WECOM_FORWARD_PLUS_`` prefix (typically loaded from a ``.env`` file via
python-dotenv). Groups are discovered by scanning indexed variables:

- ``WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_ID``    (required per group)
- ``WECOM_FORWARD_PLUS_GROUP_{N}_WECOM_ROBOT_SECRET`` (required per group)
- ``WECOM_FORWARD_PLUS_GROUP_{N}_DIFY_API_KEY``      (required per group)
- ``WECOM_FORWARD_PLUS_GROUP_{N}_NAME``              (optional, default ``group-{N}``)
- ``WECOM_FORWARD_PLUS_GROUP_{N}_SESSION_MAX_TOTAL`` (optional, overrides global)

Indices are 1-based and must be contiguous. Any incomplete or gapped group, or
any missing required variable, raises :class:`ConfigError` so the process can
exit with a clear message before connecting to anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional

PREFIX = "WECOM_FORWARD_PLUS_"

DEFAULT_SESSION_TTL_SECONDS = 300
DEFAULT_SESSION_MAX_TOTAL = 200
DEFAULT_RESET_KEYWORDS = ["开启新对话", "重置对话", "新一轮对话"]

# Stop scanning group indices at this upper bound.
_MAX_GROUP_INDEX = 1000


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class GroupConfig:
    """One WeCom robot <-> one Dify app API key, plus its session cap."""

    index: int
    name: str
    wecom_robot_id: str
    wecom_robot_secret: str
    dify_api_key: str
    session_max_total: int


@dataclass
class Config:
    """Fully validated runtime configuration."""

    dify_base_url: str
    session_ttl_seconds: int
    session_max_total: int
    reset_keywords: List[str]
    groups: List[GroupConfig] = field(default_factory=list)

    def get_group(self, group_id: str) -> GroupConfig:
        for group in self.groups:
            if group.name == group_id:
                return group
        raise KeyError(f"Unknown group: {group_id}")


def _parse_int(value: str, var_name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{var_name} must be an integer, got {value!r}") from None


def _int_env(env: Mapping[str, str], suffix: str, default: int) -> int:
    raw = env.get(PREFIX + suffix)
    if raw is None or str(raw).strip() == "":
        return default
    return _parse_int(raw, PREFIX + suffix)


def _int_env_direct(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return _parse_int(raw, key)


def _parse_keywords(raw: Optional[str]) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return list(DEFAULT_RESET_KEYWORDS)

    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{PREFIX}SESSION_RESET_KEYWORDS is not valid JSON: {raw!r}"
            ) from exc
        if not isinstance(parsed, list):
            raise ConfigError(
                f"{PREFIX}SESSION_RESET_KEYWORDS must be a JSON array "
                "or a comma-separated string"
            )
        values = [str(v).strip() for v in parsed if str(v).strip()]
        return values

    return [v.strip() for v in raw.split(",") if v.strip()]


def _parse_groups(env: Mapping[str, str], default_max: int) -> List[GroupConfig]:
    groups: List[GroupConfig] = []
    seen_missing = False

    for n in range(1, _MAX_GROUP_INDEX + 1):
        robot_id = (env.get(f"{PREFIX}GROUP_{n}_WECOM_ROBOT_ID") or "").strip()
        robot_secret = (env.get(f"{PREFIX}GROUP_{n}_WECOM_ROBOT_SECRET") or "").strip()
        dify_key = (env.get(f"{PREFIX}GROUP_{n}_DIFY_API_KEY") or "").strip()

        present = [v for v in (robot_id, robot_secret, dify_key) if v]
        if not present:
            seen_missing = True
            continue
        if len(present) != 3:
            raise ConfigError(
                f"Group {n} is incomplete: WECOM_ROBOT_ID, WECOM_ROBOT_SECRET "
                "and DIFY_API_KEY are all required"
            )
        if seen_missing:
            raise ConfigError(
                f"Group indices must be contiguous; group {n} is set but an "
                "earlier index is missing"
            )

        name = (env.get(f"{PREFIX}GROUP_{n}_NAME") or "").strip() or f"group-{n}"
        session_max = _int_env_direct(
            env, f"{PREFIX}GROUP_{n}_SESSION_MAX_TOTAL", default_max
        )
        if session_max <= 0:
            raise ConfigError(
                f"{PREFIX}GROUP_{n}_SESSION_MAX_TOTAL must be positive"
            )

        groups.append(
            GroupConfig(
                index=n,
                name=name,
                wecom_robot_id=robot_id,
                wecom_robot_secret=robot_secret,
                dify_api_key=dify_key,
                session_max_total=session_max,
            )
        )

    if not groups:
        raise ConfigError(
            "No groups configured; set at least WECOM_FORWARD_PLUS_GROUP_1_* "
            "variables"
        )

    seen_names = set()
    for group in groups:
        if group.name in seen_names:
            raise ConfigError(f"Duplicate group name: {group.name}")
        seen_names.add(group.name)

    return groups


def load_config(environ: Optional[Mapping[str, str]] = None) -> Config:
    """Parse and validate configuration from ``environ`` (default: ``os.environ``)."""
    env = os.environ if environ is None else environ

    base_url = (env.get(PREFIX + "DIFY_BASE_URL") or "").strip()
    if not base_url:
        raise ConfigError(f"{PREFIX}DIFY_BASE_URL is required")
    base_url = base_url.rstrip("/")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError(
            f"{PREFIX}DIFY_BASE_URL must start with http:// or https://"
        )

    ttl = _int_env(env, "SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)
    if ttl <= 0:
        raise ConfigError(f"{PREFIX}SESSION_TTL_SECONDS must be positive")

    max_total = _int_env(env, "SESSION_MAX_TOTAL", DEFAULT_SESSION_MAX_TOTAL)
    if max_total <= 0:
        raise ConfigError(f"{PREFIX}SESSION_MAX_TOTAL must be positive")

    keywords = _parse_keywords(env.get(PREFIX + "SESSION_RESET_KEYWORDS"))
    groups = _parse_groups(env, max_total)

    return Config(
        dify_base_url=base_url,
        session_ttl_seconds=ttl,
        session_max_total=max_total,
        reset_keywords=keywords,
        groups=groups,
    )
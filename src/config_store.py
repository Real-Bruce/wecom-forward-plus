"""Mutable in-process registry of group configurations.

:class:`GroupStore` maps group names to :class:`GroupConfig` instances and is
the runtime source the message handler consults for every incoming message.
It lives on the asyncio event loop and has a single writer (the reconcile
loop applying database state), so no locking is needed; readers always see a
fully-formed :class:`GroupConfig` because entries are replaced atomically.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from .config import Config, GroupConfig


class GroupStore:
    """Group name -> GroupConfig registry, updated at runtime."""

    def __init__(self, groups: Optional[Mapping[str, GroupConfig]] = None) -> None:
        self._groups: Dict[str, GroupConfig] = dict(groups or {})

    @classmethod
    def from_config(cls, config: Config) -> "GroupStore":
        """Build a store holding every group of a static env configuration."""
        return cls({group.name: group for group in config.groups})

    def get(self, group_id: str) -> GroupConfig:
        group = self._groups.get(group_id)
        if group is None:
            raise KeyError(f"Unknown group: {group_id}")
        return group

    def put(self, group: GroupConfig) -> None:
        self._groups[group.name] = group

    def remove(self, group_id: str) -> None:
        self._groups.pop(group_id, None)

    def snapshot(self) -> Dict[str, GroupConfig]:
        """Shallow copy for diffing outside the store."""
        return dict(self._groups)

    def __len__(self) -> int:
        return len(self._groups)

"""Immutable description of one media item extracted from a WeCom message.

Produced by :mod:`src.wecom_client` (which owns the SDK download), consumed by
:mod:`src.message_handler` (which uploads it to Dify). Living in its own module
keeps the import direction wecom_client -> message_handler one-way.
"""

from __future__ import annotations

from dataclasses import dataclass

# Dify file categories an incoming WeCom media item can map to ("files[].type").
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"


@dataclass(frozen=True)
class Attachment:
    """One downloaded media file with the Dify category it should be sent as."""

    filename: str  # always non-empty; producer applies a fallback when WeCom gives none
    data: bytes
    kind: str  # KIND_IMAGE | KIND_DOCUMENT

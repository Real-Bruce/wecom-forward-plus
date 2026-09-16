"""Thin wrapper around wecom-aibot-python-sdk's long-connection bot.

One :class:`WeComClient` maps to one configured group and therefore one WeCom
robot. It extracts the sender and text content from incoming frames, delegates
to a handler coroutine, and sends the produced reply back through the same
WebSocket frame.

Reconnect (exponential backoff capped at 30s) is delegated to the SDK by
setting ``max_reconnect_attempts=-1``; the ``disconnected`` / ``reconnecting``
/ ``error`` events are surfaced here only for logging.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from aibot import WSClient, WSClientOptions, generate_req_id

from .constants import ERROR_REPLY

logger = logging.getLogger(__name__)

# Signature of the message handler: (from_user, content) -> reply text.
Handler = Callable[[str, str], Awaitable[str]]

_SEND_RETRY_ATTEMPTS = 3
_SEND_RETRY_DELAY_SECONDS = 1.0


class WeComClient:
    """Long-connection client for one WeCom robot."""

    def __init__(
        self,
        group_id: str,
        robot_id: str,
        robot_secret: str,
        handler: Handler,
    ) -> None:
        self._group_id = group_id
        self._handler = handler
        self._client = WSClient(
            WSClientOptions(
                bot_id=robot_id,
                secret=robot_secret,
                max_reconnect_attempts=-1,  # infinite reconnect with backoff
            )
        )

        self._client.on("message.text", self._on_text)
        self._client.on("message.voice", self._on_voice)
        self._client.on("disconnected", self._on_disconnected)
        self._client.on("reconnecting", self._on_reconnecting)
        self._client.on("error", self._on_error)

    # -- frame handlers --------------------------------------------------

    async def _on_text(self, frame) -> None:
        body = frame.get("body") or {}
        await self._route(frame, (body.get("text") or {}).get("content", ""))

    async def _on_voice(self, frame) -> None:
        # Voice messages are auto-transcribed by WeCom; reuse the same path.
        body = frame.get("body") or {}
        await self._route(frame, (body.get("voice") or {}).get("content", ""))

    def _on_disconnected(self, reason) -> None:
        logger.warning(
            "WeCom connection lost (group=%s): %s", self._group_id, reason
        )

    def _on_reconnecting(self, attempt) -> None:
        logger.info(
            "WeCom reconnecting (group=%s), attempt %s", self._group_id, attempt
        )

    def _on_error(self, error) -> None:
        logger.error("WeCom client error (group=%s): %s", self._group_id, error)

    # -- core flow -------------------------------------------------------

    async def _route(self, frame, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return

        from_user = self._extract_user(frame.get("body") or {})

        reply = ERROR_REPLY
        try:
            reply = await self._handler(from_user, content)
        except Exception:
            logger.exception(
                "Handler failed for group=%s user=%s", self._group_id, from_user
            )

        if reply:
            await self._send_reply(frame, reply)

    @staticmethod
    def _extract_user(body: dict) -> str:
        sender = body.get("from") or {}
        return sender.get("userid") or sender.get("name") or ""

    async def _send_reply(self, frame, text: str) -> None:
        stream_id = generate_req_id("stream")
        last_exc: Optional[Exception] = None
        for attempt in range(1, _SEND_RETRY_ATTEMPTS + 1):
            try:
                await self._client.reply_stream(frame, stream_id, text, finish=True)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Reply send failed for group=%s (attempt %d/%d): %s",
                    self._group_id,
                    attempt,
                    _SEND_RETRY_ATTEMPTS,
                    exc,
                )
                if attempt < _SEND_RETRY_ATTEMPTS:
                    await asyncio.sleep(_SEND_RETRY_DELAY_SECONDS)
        logger.error("Giving up on reply for group=%s: %s", self._group_id, last_exc)

    # -- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        await self._client.connect()

    def disconnect(self) -> None:
        self._client.disconnect()
"""Thin wrapper around wecom-aibot-python-sdk's long-connection bot.

One :class:`WeComClient` maps to one configured group and therefore one WeCom
robot. It extracts the sender and text content from incoming frames, delegates
to a handler coroutine, and sends the produced reply back through the same
WebSocket frame.

Media messages (image / file / mixed) are downloaded and decrypted here —
only this class holds the SDK client, and the WeCom media URLs expire after
~5 minutes, so the download must happen inside the event handler before any
Dify awaits. Voice messages are auto-transcribed by WeCom and reuse the text
path.

Reconnect (exponential backoff capped at 30s) is delegated to the SDK by
setting ``max_reconnect_attempts=-1``; the ``disconnected`` / ``reconnecting``
/ ``error`` events are surfaced here only for logging.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Mapping, Optional, Sequence

from aibot import WSClient, WSClientOptions, generate_req_id

from .attachment import Attachment, KIND_DOCUMENT, KIND_IMAGE
from .constants import ERROR_REPLY, FILE_DOWNLOAD_FAILED_REPLY

logger = logging.getLogger(__name__)

# Signature of the message handler: (from_user, content, attachments) -> reply.
Handler = Callable[[str, str, Sequence[Attachment]], Awaitable[str]]

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
        self._client.on("message.image", self._on_image)
        self._client.on("message.file", self._on_file)
        self._client.on("message.mixed", self._on_mixed)
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

    async def _on_image(self, frame) -> None:
        body = frame.get("body") or {}
        await self._route_media(
            frame, [body.get("image") or {}], KIND_IMAGE, "image.png"
        )

    async def _on_file(self, frame) -> None:
        body = frame.get("body") or {}
        await self._route_media(frame, [body.get("file") or {}], KIND_DOCUMENT, "file")

    async def _on_mixed(self, frame) -> None:
        body = frame.get("body") or {}
        # The Python SDK does not document the mixed schema; the Node SDK types
        # say each item carries "msgtype" plus a "text" or "image" sub-object.
        # "type" is accepted as a defensive alias against schema drift.
        items = (body.get("mixed") or {}).get("msg_item") or []
        text_parts = []
        media_list = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("msgtype") or item.get("type")
            if item_type == "text":
                text_parts.append((item.get("text") or {}).get("content") or "")
            elif item_type == "image":
                media_list.append(item.get("image") or {})
            else:
                logger.debug("Skipping unknown mixed item type: %r", item_type)
        await self._route_media(
            frame, media_list, KIND_IMAGE, "image-1.png", content="\n".join(text_parts)
        )

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

    async def _route(
        self, frame, content: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        content = (content or "").strip()
        if not content and not attachments:
            return

        from_user = self._extract_user(frame.get("body") or {})

        reply = ERROR_REPLY
        try:
            reply = await self._handler(from_user, content, tuple(attachments))
        except Exception:
            logger.exception(
                "Handler failed for group=%s user=%s", self._group_id, from_user
            )

        if reply:
            await self._send_reply(frame, reply)

    async def _route_media(
        self,
        frame,
        media_list: Sequence[Mapping],
        kind: str,
        fallback_name: str,
        content: str = "",
    ) -> None:
        """Download every media item, then route text + attachments together.

        A download failure short-circuits the whole message (partial forwarding
        of a mixed message would be more confusing than useful), even when the
        remaining text content is non-empty.
        """
        attachments = []
        try:
            for index, media in enumerate(media_list, start=1):
                fallback = (
                    fallback_name if len(media_list) == 1 else f"image-{index}.png"
                )
                attachments.append(await self._download_media(media, kind, fallback))
        except Exception:
            logger.exception("Media download failed for group=%s", self._group_id)
            await self._send_reply(frame, FILE_DOWNLOAD_FAILED_REPLY)
            return

        await self._route(frame, content, attachments)

    async def _download_media(
        self, media: Mapping, kind: str, fallback_name: str
    ) -> Attachment:
        """Download and decrypt one WeCom media item into an Attachment."""
        if not media.get("url"):
            raise ValueError("media item has no url")
        data, filename = await self._client.download_file(
            media.get("url"), media.get("aeskey")
        )
        name = (filename or "").strip().strip('"') or fallback_name
        return Attachment(filename=name, data=data, kind=kind)

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
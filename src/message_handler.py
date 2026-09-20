"""Route WeCom messages to Dify and produce the outgoing reply text."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Sequence

from .attachment import Attachment, KIND_IMAGE
from .config import Config
from .constants import (
    ERROR_REPLY,
    FILE_DEFAULT_QUERY,
    FILE_EMPTY_FILE_REPLY,
    FILE_TOO_LARGE_REPLY,
    FILE_UPLOAD_FAILED_REPLY,
    RESET_REPLY,
)
from .dify_client import (
    DifyClient,
    DifyError,
    MAX_DOCUMENT_FILE_BYTES,
    MAX_IMAGE_FILE_BYTES,
)
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


class MessageHandler:
    """Turn an incoming WeCom user message into a reply.

    Reset keywords short-circuit Dify and start a fresh session for the user.
    Otherwise the handler reads (or creates) the user's session for the group,
    calls the Dify streaming API, persists the returned ``conversation_id`` and
    returns the accumulated answer.

    Messages carrying attachments (images, files, mixed) are validated locally,
    uploaded to Dify under the same ``wx_<account>`` user that later references
    them, and sent with the ``files`` parameter.
    """

    def __init__(
        self,
        config: Config,
        session_manager: SessionManager,
        dify_client: DifyClient,
    ) -> None:
        self._config = config
        self._sessions = session_manager
        self._dify = dify_client
        self._reset_keywords = set(config.reset_keywords)

    def handler_for(
        self, group_id: str
    ) -> Callable[[str, str, Sequence[Attachment]], Awaitable[str]]:
        """Return a ``(from_user, content, attachments) -> reply`` coroutine."""

        async def _handle(
            from_user: str, content: str, attachments: Sequence[Attachment] = ()
        ) -> str:
            return await self.handle(group_id, from_user, content, attachments)

        return _handle

    async def handle(
        self,
        group_id: str,
        from_user: str,
        content: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        # Built once and shared by upload + chat: Dify ties uploaded files to
        # the requesting user, so both calls must carry the same identity.
        dify_user = f"wx_{from_user}"
        content = content.strip()

        # A reset keyword mixed with attachments (e.g. "新对话" + screenshot)
        # still forwards: the user clearly wants the file handled.
        if not attachments and content in self._reset_keywords:
            self._sessions.reset(group_id, dify_user)
            logger.info("Session reset for group=%s user=%s", group_id, dify_user)
            return RESET_REPLY

        group = self._config.get_group(group_id)
        session = self._sessions.get_or_create(group_id, dify_user)

        files = None
        if attachments:
            files, error_reply = await self._upload_attachments(
                group_id, dify_user, group.dify_api_key, attachments
            )
            if error_reply:
                return error_reply
            query = content or FILE_DEFAULT_QUERY
        else:
            query = content

        try:
            result = await self._dify.send_chat_message(
                api_key=group.dify_api_key,
                query=query,
                user=dify_user,
                conversation_id=session.conversation_id,
                files=files,
            )
        except DifyError as exc:
            logger.warning(
                "Dify call failed for group=%s user=%s: %s", group_id, dify_user, exc
            )
            return ERROR_REPLY

        if result.conversation_id:
            session.conversation_id = result.conversation_id

        if not result.answer:
            logger.warning(
                "Dify returned an empty answer for group=%s user=%s", group_id, dify_user
            )
            return ERROR_REPLY

        return result.answer

    async def _upload_attachments(
        self,
        group_id: str,
        dify_user: str,
        api_key: str,
        attachments: Sequence[Attachment],
    ):
        """Validate and upload attachments.

        Returns ``(file_refs, None)`` on success or ``([], error_reply)`` when a
        local guard or the upload failed.
        """
        for attachment in attachments:
            if not attachment.data:
                logger.warning(
                    "Empty attachment (%s) for group=%s user=%s",
                    attachment.filename,
                    group_id,
                    dify_user,
                )
                return [], FILE_EMPTY_FILE_REPLY
            limit = (
                MAX_IMAGE_FILE_BYTES
                if attachment.kind == KIND_IMAGE
                else MAX_DOCUMENT_FILE_BYTES
            )
            if len(attachment.data) > limit:
                logger.warning(
                    "Attachment %s exceeds %d bytes for group=%s user=%s",
                    attachment.filename,
                    limit,
                    group_id,
                    dify_user,
                )
                return [], FILE_TOO_LARGE_REPLY

        refs = []
        try:
            for attachment in attachments:
                uploaded = await self._dify.upload_file(
                    api_key=api_key,
                    user=dify_user,
                    filename=attachment.filename,
                    data=attachment.data,
                )
                refs.append(
                    {
                        "type": attachment.kind,
                        "transfer_method": "local_file",
                        "upload_file_id": uploaded.id,
                    }
                )
        except DifyError as exc:
            logger.warning(
                "Dify upload failed for group=%s user=%s: %s", group_id, dify_user, exc
            )
            if exc.status == 413:
                return [], FILE_TOO_LARGE_REPLY
            return [], FILE_UPLOAD_FAILED_REPLY

        return refs, None
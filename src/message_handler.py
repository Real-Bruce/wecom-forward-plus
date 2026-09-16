"""Route WeCom messages to Dify and produce the outgoing reply text."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .config import Config
from .constants import ERROR_REPLY, RESET_REPLY
from .dify_client import DifyClient, DifyError
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


class MessageHandler:
    """Turn an incoming WeCom user message into a reply.

    Reset keywords short-circuit Dify and start a fresh session for the user.
    Otherwise the handler reads (or creates) the user's session for the group,
    calls the Dify streaming API, persists the returned ``conversation_id`` and
    returns the accumulated answer.
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

    def handler_for(self, group_id: str) -> Callable[[str, str], Awaitable[str]]:
        """Return a ``(from_user, content) -> reply`` coroutine bound to a group."""

        async def _handle(from_user: str, content: str) -> str:
            return await self.handle(group_id, from_user, content)

        return _handle

    async def handle(self, group_id: str, from_user: str, content: str) -> str:
        dify_user = f"wx_{from_user}"
        content = content.strip()

        if content in self._reset_keywords:
            self._sessions.reset(group_id, dify_user)
            logger.info("Session reset for group=%s user=%s", group_id, dify_user)
            return RESET_REPLY

        group = self._config.get_group(group_id)
        session = self._sessions.get_or_create(group_id, dify_user)

        try:
            result = await self._dify.send_chat_message(
                api_key=group.dify_api_key,
                query=content,
                user=dify_user,
                conversation_id=session.conversation_id,
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
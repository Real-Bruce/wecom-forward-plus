"""Async client for the Dify chat-messages API (streaming mode).

Only the streaming response mode is supported in this version. The client
POSTs ``/chat-messages`` and reduces the Server-Sent Events stream into a final
answer plus the ``conversation_id``, which the caller stores to keep a
multi-turn conversation alive.

The pure parsing helpers (:func:`iter_sse_events` / :func:`accumulate_stream`)
are separated from the aiohttp transport so they can be unit-tested without any
network access.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Iterator, Optional

import aiohttp


class DifyError(Exception):
    """Raised when the Dify request fails or the stream reports an error."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class DifyStreamResult:
    """Outcome of a streaming chat-messages call."""

    answer: str
    conversation_id: Optional[str] = None


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict]:
    """Yield the JSON payloads carried by ``data:`` lines of an SSE stream.

    Non-``data:`` lines (such as the ``event: ping`` keep-alive) and empty
    payloads are ignored. Malformed JSON is skipped rather than raised so a
    single bad chunk cannot abort the whole reply.
    """
    for line in lines:
        line = line.rstrip("\r\n")
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def accumulate_stream(events: Iterable[dict]) -> DifyStreamResult:
    """Reduce parsed Dify events into a final answer and ``conversation_id``.

    Rules (see Dify ``send-chat-message`` streaming docs):

    * ``message`` events carry answer fragments for chat assistants and
      Chatflow apps; they are concatenated in order.
    * ``agent_message`` events carry fragments for Agent apps. A trailing
      ``message`` event then repeats the complete answer and is used verbatim
      instead of being appended.
    * ``message_replace`` replaces the answer accumulated so far.
    * ``conversation_id`` is present on every event; the last non-empty value
      wins.
    * An ``error`` event ends the stream in failure.
    """
    parts = []
    conversation_id: Optional[str] = None
    saw_agent_message = False

    for event in events:
        cid = event.get("conversation_id")
        if cid:
            conversation_id = cid

        etype = event.get("event")
        if etype == "error":
            code = event.get("code")
            message = event.get("message") or "unknown error"
            detail = f"{code}: {message}" if code else str(message)
            raise DifyError(detail, status=event.get("status"))
        if etype == "agent_message":
            saw_agent_message = True
            parts.append(event.get("answer") or "")
        elif etype == "message":
            if saw_agent_message:
                parts = [event.get("answer") or ""]
            else:
                parts.append(event.get("answer") or "")
        elif etype == "message_replace":
            parts = [event.get("answer") or ""]

    return DifyStreamResult(answer="".join(parts), conversation_id=conversation_id)


class DifyClient:
    """Thin async wrapper around ``POST /chat-messages`` (streaming)."""

    def __init__(
        self,
        base_url: str,
        total_timeout: float = 300.0,
        read_timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # ``sock_read`` is a per-read timeout, kept well above Dify's ~10s
        # keep-alive ping so long streams are not spuriously cut.
        self._timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=10.0,
            sock_read=read_timeout,
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def send_chat_message(
        self,
        api_key: str,
        query: str,
        user: str,
        conversation_id: Optional[str] = None,
    ) -> DifyStreamResult:
        """Send one message and return the accumulated answer + conversation id."""
        url = f"{self._base_url}/chat-messages"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": {},
            "query": query,
            "user": user,
            "response_mode": "streaming",
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        session = await self._get_session()
        events = []
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise DifyError(
                        await self._format_error_response(resp), status=resp.status
                    )
                async for event in self._iter_response_events(resp):
                    events.append(event)
        except DifyError:
            raise
        except asyncio.TimeoutError:
            raise DifyError("Dify request timed out") from None
        except aiohttp.ClientError as exc:
            raise DifyError(f"Dify request failed: {exc}") from exc

        return accumulate_stream(events)

    async def _iter_response_events(self, resp) -> AsyncIterator[dict]:
        async for raw in resp.content:
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            line = line.rstrip("\r\n")
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield data

    async def _format_error_response(self, resp) -> str:
        """Build a sanitized error string without echoing raw bodies."""
        text = ""
        try:
            text = await resp.text()
        except Exception:
            pass
        try:
            body = json.loads(text)
            code = body.get("code")
            message = body.get("message")
            if code:
                return f"{code}: {message}" if message else str(code)
            if message:
                return str(message)
        except (ValueError, TypeError):
            pass
        return f"HTTP {resp.status}"

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
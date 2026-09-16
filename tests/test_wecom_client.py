"""Tests for the WeCom client routing glue (no real WebSocket needed)."""

from src.constants import ERROR_REPLY
from src.wecom_client import WeComClient


def _client(handler):
    # Constructing a WSClient does not connect; only credentials are stored.
    return WeComClient("group-1", "robot-id", "robot-secret", handler)


async def test_extract_user_prefers_userid():
    body = {"from": {"userid": "zhangsan", "name": "张三"}}
    assert WeComClient._extract_user(body) == "zhangsan"


async def test_extract_user_falls_back_to_name():
    assert WeComClient._extract_user({"from": {"name": "张三"}}) == "张三"
    assert WeComClient._extract_user({}) == ""


async def test_route_extracts_sender_and_sends_reply():
    sent = []

    async def handler(from_user, content):
        return f"echo:{from_user}:{content}"

    async def capture(frame, text):
        sent.append(text)

    client = _client(handler)
    client._send_reply = capture  # bypass the real SDK reply

    await client._route(
        {"body": {"from": {"userid": "zhangsan"}, "text": {"content": "hi"}}},
        "hi",
    )

    assert sent == ["echo:zhangsan:hi"]


async def test_route_ignores_empty_content():
    called = []

    async def handler(from_user, content):
        called.append(content)
        return "x"

    async def capture(frame, text):
        raise AssertionError("should not reply to empty content")

    client = _client(handler)
    client._send_reply = capture

    await client._route({}, "   ")

    assert called == []


async def test_route_falls_back_to_error_reply_on_exception():
    async def handler(from_user, content):
        raise RuntimeError("boom")

    client = _client(handler)

    reply = []
    async def capture(frame, text):
        reply.append(text)

    client._send_reply = capture

    await client._route(
        {"body": {"from": {"userid": "zhangsan"}, "text": {"content": "hi"}}},
        "hi",
    )

    assert reply == [ERROR_REPLY]
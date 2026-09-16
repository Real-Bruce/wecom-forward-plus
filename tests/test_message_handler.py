"""Tests for the message routing logic (reset keyword + Dify call)."""

from src.config import Config, GroupConfig
from src.constants import ERROR_REPLY, RESET_REPLY
from src.dify_client import DifyError, DifyStreamResult
from src.message_handler import MessageHandler
from src.session_manager import SessionManager


class RecordingDifyClient:
    """Stand-in for DifyClient that records calls and returns canned results."""

    def __init__(self, results=None, error=None):
        self.results = list(results) if results else []
        self.error = error
        self.calls = []

    async def send_chat_message(self, api_key, query, user, conversation_id=None):
        self.calls.append(
            {
                "api_key": api_key,
                "query": query,
                "user": user,
                "conversation_id": conversation_id,
            }
        )
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


def _config(reset_keywords=("开启新对话",)):
    groups = [
        GroupConfig(
            index=1,
            name="group-1",
            wecom_robot_id="bot-1",
            wecom_robot_secret="sec-1",
            dify_api_key="app-1",
            session_max_total=200,
        )
    ]
    return Config(
        dify_base_url="https://dify.example.com/v1",
        session_ttl_seconds=300,
        session_max_total=200,
        reset_keywords=list(reset_keywords),
        groups=groups,
    )


def _handler(config, dify_client):
    sessions = SessionManager(config)
    return MessageHandler(config, sessions, dify_client), sessions


async def test_reset_keyword_short_circuits_dify():
    dify = RecordingDifyClient()
    handler, sessions = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "开启新对话")

    assert reply == RESET_REPLY
    assert dify.calls == []  # Dify was never called
    assert sessions.size() == 0  # no session was created


async def test_reset_keyword_adds_wx_prefix_and_matches_stripped():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="ok")])
    handler, sessions = _handler(_config(), dify)

    await handler.handle("group-1", "zhangsan", "hi")
    assert dify.calls[0]["user"] == "wx_zhangsan"
    assert sessions.size() == 1

    # A reset keyword with surrounding whitespace still matches.
    reply = await handler.handle("group-1", "zhangsan", "  开启新对话  ")

    assert reply == RESET_REPLY
    assert sessions.size() == 0  # session reset and removed


async def test_normal_message_calls_dify_and_stores_conversation():
    dify = RecordingDifyClient(
        results=[
            DifyStreamResult(answer="你好", conversation_id="conv-1"),
            DifyStreamResult(answer="世界", conversation_id="conv-1"),
        ]
    )
    handler, sessions = _handler(_config(), dify)

    first = await handler.handle("group-1", "zhangsan", "你好")
    session = sessions.get_or_create("group-1", "wx_zhangsan")

    assert first == "你好"
    assert dify.calls[0]["conversation_id"] is None  # fresh conversation
    assert dify.calls[0]["api_key"] == "app-1"
    assert session.conversation_id == "conv-1"

    second = await handler.handle("group-1", "zhangsan", "再来一句")
    assert second == "世界"
    assert dify.calls[1]["conversation_id"] == "conv-1"  # continued conversation


async def test_dify_error_returns_fallback_reply():
    dify = RecordingDifyClient(error=DifyError("Dify request timed out"))
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "你好")

    assert reply == ERROR_REPLY


async def test_empty_answer_returns_fallback_reply():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="", conversation_id="conv-1")])
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "你好")

    assert reply == ERROR_REPLY
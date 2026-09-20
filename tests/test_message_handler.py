"""Tests for the message routing logic (reset keyword + Dify call)."""

from src.attachment import Attachment, KIND_DOCUMENT, KIND_IMAGE
from src.config import Config, GroupConfig
from src.constants import (
    ERROR_REPLY,
    FILE_DEFAULT_QUERY,
    FILE_EMPTY_FILE_REPLY,
    FILE_TOO_LARGE_REPLY,
    FILE_UPLOAD_FAILED_REPLY,
    RESET_REPLY,
)
from src.dify_client import (
    DifyError,
    DifyStreamResult,
    DifyUploadedFile,
    MAX_IMAGE_FILE_BYTES,
)
from src.message_handler import MessageHandler
from src.session_manager import SessionManager


class RecordingDifyClient:
    """Stand-in for DifyClient that records calls and returns canned results."""

    def __init__(self, results=None, error=None, upload_error=None):
        self.results = list(results) if results else []
        self.error = error
        self.upload_error = upload_error
        self.calls = []
        self.uploads = []

    async def send_chat_message(
        self, api_key, query, user, conversation_id=None, files=None
    ):
        self.calls.append(
            {
                "api_key": api_key,
                "query": query,
                "user": user,
                "conversation_id": conversation_id,
                "files": files,
            }
        )
        if self.error is not None:
            raise self.error
        return self.results.pop(0)

    async def upload_file(self, api_key, user, filename, data, content_type=None):
        self.uploads.append(
            {
                "api_key": api_key,
                "user": user,
                "filename": filename,
                "data": data,
                "content_type": content_type,
            }
        )
        if self.upload_error is not None:
            raise self.upload_error
        return DifyUploadedFile(id=f"fid-{len(self.uploads)}", name=filename)


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
    assert dify.calls[0]["files"] is None  # text-only path unchanged
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


# -- attachments ------------------------------------------------------------


def _image(data=b"img", filename="a.png"):
    return Attachment(filename, data, KIND_IMAGE)


async def test_file_only_uses_default_query_and_consistent_user():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "", [_image()])

    assert reply == "done"
    assert len(dify.uploads) == 1
    assert dify.uploads[0]["api_key"] == "app-1"
    assert dify.uploads[0]["user"] == "wx_zhangsan"
    assert dify.uploads[0]["filename"] == "a.png"
    assert dify.uploads[0]["data"] == b"img"
    assert dify.calls[0]["query"] == FILE_DEFAULT_QUERY
    assert dify.calls[0]["user"] == dify.uploads[0]["user"]  # same owner
    assert dify.calls[0]["files"] == [
        {"type": "image", "transfer_method": "local_file", "upload_file_id": "fid-1"}
    ]


async def test_mixed_text_and_image_forwards_both():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "看看这张图", [_image()])

    assert reply == "done"
    assert dify.calls[0]["query"] == "看看这张图"
    assert len(dify.calls[0]["files"]) == 1


async def test_image_over_limit_fails_fast():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, _ = _handler(_config(), dify)

    big = _image(data=b"x" * (MAX_IMAGE_FILE_BYTES + 1))
    reply = await handler.handle("group-1", "zhangsan", "", [big])

    assert reply == FILE_TOO_LARGE_REPLY
    assert dify.uploads == []  # no upload attempted
    assert dify.calls == []  # no chat call either


async def test_document_over_image_limit_but_under_own_limit_passes():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, _ = _handler(_config(), dify)

    doc = Attachment("报告.docx", b"x" * (MAX_IMAGE_FILE_BYTES + 1), KIND_DOCUMENT)
    reply = await handler.handle("group-1", "zhangsan", "", [doc])

    assert reply == "done"
    assert len(dify.uploads) == 1


async def test_empty_attachment_returns_empty_reply():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "", [_image(data=b"")])

    assert reply == FILE_EMPTY_FILE_REPLY
    assert dify.uploads == []
    assert dify.calls == []


async def test_upload_error_returns_upload_failed():
    dify = RecordingDifyClient(
        results=[DifyStreamResult(answer="done")],
        upload_error=DifyError("Dify file upload failed: boom"),
    )
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "", [_image()])

    assert reply == FILE_UPLOAD_FAILED_REPLY
    assert dify.calls == []


async def test_upload_413_maps_to_too_large():
    dify = RecordingDifyClient(
        results=[DifyStreamResult(answer="done")],
        upload_error=DifyError("payload too large", status=413),
    )
    handler, _ = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "", [_image()])

    assert reply == FILE_TOO_LARGE_REPLY


async def test_reset_keyword_with_attachment_still_forwards():
    dify = RecordingDifyClient(results=[DifyStreamResult(answer="done")])
    handler, sessions = _handler(_config(), dify)

    reply = await handler.handle("group-1", "zhangsan", "开启新对话", [_image()])

    assert reply == "done"
    assert dify.calls[0]["query"] == "开启新对话"  # text used, not the default
    assert len(dify.calls[0]["files"]) == 1
    assert sessions.size() == 1  # no reset happened

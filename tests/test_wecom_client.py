"""Tests for the WeCom client routing glue (no real WebSocket needed)."""

from src.attachment import Attachment, KIND_DOCUMENT, KIND_IMAGE
from src.constants import ERROR_REPLY, FILE_DOWNLOAD_FAILED_REPLY
from src.wecom_client import WeComClient


class FakeSdkClient:
    """Stand-in for aibot.WSClient exposing only what routing touches."""

    def __init__(self, downloads=None, error=None):
        # downloads maps url -> (data, filename); error is raised instead.
        self.downloads = downloads or {}
        self.error = error

    async def download_file(self, url, aes_key):
        if self.error is not None:
            raise self.error
        return self.downloads[url]


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

    async def handler(from_user, content, attachments=()):
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


async def test_route_ignores_empty_content_without_attachments():
    called = []

    async def handler(from_user, content, attachments=()):
        called.append(content)
        return "x"

    async def capture(frame, text):
        raise AssertionError("should not reply to empty content")

    client = _client(handler)
    client._send_reply = capture

    await client._route({}, "   ")

    assert called == []


async def test_route_falls_back_to_error_reply_on_exception():
    async def handler(from_user, content, attachments=()):
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


# -- media (image / file / mixed) ------------------------------------------


def _media_client(handler, downloads=None, error=None):
    client = _client(handler)
    client._client = FakeSdkClient(downloads, error)
    return client


async def test_on_image_downloads_and_forwards_attachment():
    received = []

    async def handler(from_user, content, attachments=()):
        received.append((from_user, content, attachments))
        return "ok"

    sent = []

    async def capture(frame, text):
        sent.append(text)

    client = _media_client(handler, downloads={"u": (b"pngdata", "pic.png")})
    client._send_reply = capture

    await client._on_image(
        {"body": {"from": {"userid": "zhangsan"}, "image": {"url": "u", "aeskey": "k"}}}
    )

    from_user, content, attachments = received[0]
    assert from_user == "zhangsan"
    assert content == ""
    assert attachments == (Attachment("pic.png", b"pngdata", KIND_IMAGE),)
    assert sent == ["ok"]


async def test_on_file_maps_to_document_and_falls_back_name():
    received = []

    async def handler(from_user, content, attachments=()):
        received.append(attachments)

    client = _media_client(handler, downloads={"u": (b"data", None)})

    async def noop(frame, text):
        pass

    client._send_reply = noop

    await client._on_file(
        {"body": {"from": {"userid": "zhangsan"}, "file": {"url": "u", "aeskey": "k"}}}
    )

    assert received[0] == (Attachment("file", b"data", KIND_DOCUMENT),)


async def test_on_mixed_combines_text_and_images():
    received = []

    async def handler(from_user, content, attachments=()):
        received.append((content, attachments))

    async def noop(frame, text):
        pass

    frame = {
        "body": {
            "from": {"userid": "zhangsan"},
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "看看这张图"}},
                    {"msgtype": "image", "image": {"url": "u1", "aeskey": "k"}},
                    {"msgtype": "image", "image": {"url": "u2", "aeskey": "k"}},
                ]
            },
        }
    }
    client = _media_client(
        handler,
        downloads={"u1": (b"img1", None), "u2": (b"img2", "photo.jpg")},
    )
    client._send_reply = noop

    await client._on_mixed(frame)

    content, attachments = received[0]
    assert content == "看看这张图"
    assert attachments == (
        Attachment("image-1.png", b"img1", KIND_IMAGE),
        Attachment("photo.jpg", b"img2", KIND_IMAGE),
    )


async def test_on_mixed_accepts_legacy_type_key():
    received = []

    async def handler(from_user, content, attachments=()):
        received.append((content, attachments))

    async def noop(frame, text):
        pass

    frame = {
        "body": {
            "from": {"userid": "zhangsan"},
            "mixed": {
                "msg_item": [
                    {"type": "text", "text": {"content": "hi"}},
                    {"type": "image", "image": {"url": "u", "aeskey": "k"}},
                ]
            },
        }
    }
    client = _media_client(handler, downloads={"u": (b"img", None)})
    client._send_reply = noop

    await client._on_mixed(frame)

    content, attachments = received[0]
    assert content == "hi"
    assert attachments == (Attachment("image-1.png", b"img", KIND_IMAGE),)


async def test_download_failure_replies_and_skips_handler():
    received = []

    async def handler(from_user, content, attachments=()):
        received.append(content)
        raise AssertionError("handler must not run on download failure")

    sent = []

    async def capture(frame, text):
        sent.append(text)

    client = _media_client(handler, error=RuntimeError("download boom"))
    client._send_reply = capture

    # Mixed frame whose text part is non-empty: the failure still short-circuits.
    frame = {
        "body": {
            "from": {"userid": "zhangsan"},
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "hi"}},
                    {"msgtype": "image", "image": {"url": "u", "aeskey": "k"}},
                ]
            },
        }
    }
    await client._on_mixed(frame)

    assert received == []
    assert sent == [FILE_DOWNLOAD_FAILED_REPLY]

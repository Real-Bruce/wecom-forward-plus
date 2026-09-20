"""Tests for SSE parsing, stream accumulation and upload parsing (no network)."""

import pytest

from src.dify_client import (
    DifyError,
    DifyStreamResult,
    DifyUploadedFile,
    accumulate_stream,
    iter_sse_events,
    parse_upload_payload,
)


def test_iter_sse_events_skips_non_data_lines():
    lines = [
        "event: ping",
        "",
        'data: {"event": "message", "answer": "hi"}',
        "data:",
        "data:not-json",
    ]
    events = list(iter_sse_events(lines))
    assert events == [{"event": "message", "answer": "hi"}]


def test_iter_sse_events_handles_data_without_space():
    events = list(iter_sse_events(['data:{"event":"message","answer":"x"}']))
    assert events == [{"event": "message", "answer": "x"}]


def test_accumulate_basic_chat_fragments():
    events = [
        {"event": "message", "answer": " I", "conversation_id": "conv-1"},
        {"event": "message", "answer": " love", "conversation_id": "conv-1"},
        {"event": "message_end", "conversation_id": "conv-1"},
    ]
    result = accumulate_stream(events)
    assert result == DifyStreamResult(answer=" I love", conversation_id="conv-1")


def test_accumulate_agent_uses_final_full_message():
    events = [
        {"event": "agent_thought", "conversation_id": "conv-1"},
        {"event": "agent_message", "answer": "He", "conversation_id": "conv-1"},
        {"event": "agent_message", "answer": "llo", "conversation_id": "conv-1"},
        {"event": "message", "answer": "Hello", "conversation_id": "conv-1"},
        {"event": "message_end", "conversation_id": "conv-1"},
    ]
    result = accumulate_stream(events)
    assert result.answer == "Hello"
    assert result.conversation_id == "conv-1"


def test_accumulate_message_replace():
    events = [
        {"event": "message", "answer": "bad", "conversation_id": "conv-1"},
        {"event": "message_replace", "answer": "good", "conversation_id": "conv-1"},
    ]
    assert accumulate_stream(events).answer == "good"


def test_accumulate_no_conversation_id():
    events = [{"event": "message", "answer": "x"}]
    assert accumulate_stream(events).conversation_id is None


def test_accumulate_raises_on_error_event():
    events = [
        {"event": "message", "answer": "partial"},
        {"event": "error", "code": "invalid_param", "message": "boom", "status": 400},
    ]
    with pytest.raises(DifyError, match="invalid_param"):
        accumulate_stream(events)


def test_parse_upload_payload_maps_all_fields():
    payload = {
        "id": "a1b2c3d4",
        "name": "product-photo.png",
        "mime_type": "image/png",
        "size": 204800,
        "extension": "png",
    }
    assert parse_upload_payload(payload) == DifyUploadedFile(
        id="a1b2c3d4",
        name="product-photo.png",
        mime_type="image/png",
        size=204800,
        extension="png",
    )


def test_parse_upload_payload_ignores_extra_fields():
    uploaded = parse_upload_payload({"id": "x", "unused": 1})
    assert uploaded.id == "x"
    assert uploaded.name is None


def test_parse_upload_payload_requires_id():
    with pytest.raises(DifyError, match="missing file id"):
        parse_upload_payload({"name": "no-id.png"})
    with pytest.raises(DifyError, match="missing file id"):
        parse_upload_payload({"id": ""})


def test_parse_upload_payload_rejects_non_dict():
    with pytest.raises(DifyError, match="missing file id"):
        parse_upload_payload(None)
    with pytest.raises(DifyError, match="missing file id"):
        parse_upload_payload("oops")
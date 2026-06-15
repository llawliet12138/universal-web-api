import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.api.chat import ChatRequest
from app.api import chatgpt_thread_routes as routes


THREAD_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_router_registers_public_thread_endpoints():
    app = FastAPI()
    app.include_router(routes.router)
    paths = set(app.openapi()["paths"])

    assert "/api/chatgpt/threads" in paths
    assert "/api/chatgpt/threads/new/v1/chat/completions" in paths
    assert "/api/chatgpt/threads/{thread_id}/v1/chat/completions" in paths


class FakeRequest:
    async def is_disconnected(self):
        return False


class FakeThreadService:
    def __init__(self):
        self.ensured = []
        self.created = 0

    def list_threads(self):
        return [{"id": THREAD_ID, "title": "Test", "is_open": True, "tab_index": 4}]

    def ensure_thread_tab(self, thread_id):
        self.ensured.append(thread_id)
        return {"persistent_index": 4, "url": f"https://chatgpt.com/c/{thread_id}"}

    def create_new_thread_tab(self):
        self.created += 1
        return {"persistent_index": 5, "url": "https://chatgpt.com/"}

    def get_thread_for_tab(self, tab_index):
        return {
            "id": THREAD_ID,
            "url": f"https://chatgpt.com/c/{THREAD_ID}",
            "tab_index": tab_index,
        }


@pytest.mark.asyncio
async def test_list_threads_returns_service_data(monkeypatch):
    service = FakeThreadService()
    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)

    result = await routes.list_chatgpt_threads(authenticated=True)

    assert result == {"threads": service.list_threads(), "count": 1}


@pytest.mark.asyncio
async def test_thread_openai_route_forces_continue_preset(monkeypatch):
    service = FakeThreadService()
    captured = {}

    async def fake_chat_with_tab(tab_index, request, body, preset_name, authenticated):
        captured.update(
            tab_index=tab_index,
            body=body,
            preset_name=preset_name,
            authenticated=authenticated,
        )
        return JSONResponse({"ok": True})

    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)
    monkeypatch.setattr(routes, "chat_with_tab", fake_chat_with_tab)
    body = ChatRequest(
        messages=[
            {"role": "system", "content": "client instruction"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest question"},
        ],
        stream=True,
    )

    response = await routes.chat_in_chatgpt_thread(
        THREAD_ID,
        FakeRequest(),
        body,
        authenticated=True,
    )

    assert response.status_code == 200
    assert captured["tab_index"] == 4
    assert captured["body"].preset_name == routes.CHATGPT_CONTINUE_PRESET
    assert captured["body"].messages == [
        {"role": "user", "content": "latest question"}
    ]
    assert captured["preset_name"] == routes.CHATGPT_CONTINUE_PRESET


@pytest.mark.asyncio
async def test_existing_thread_route_requires_a_user_message(monkeypatch):
    service = FakeThreadService()
    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)

    with pytest.raises(HTTPException) as exc_info:
        await routes.chat_in_chatgpt_thread(
            THREAD_ID,
            FakeRequest(),
            ChatRequest(messages=[{"role": "system", "content": "instructions only"}]),
            authenticated=True,
        )

    assert exc_info.value.status_code == 400
    assert service.ensured == []


@pytest.mark.asyncio
async def test_thread_openai_route_rejects_invalid_thread_id(monkeypatch):
    monkeypatch.setattr(routes, "_get_thread_service", FakeThreadService)

    with pytest.raises(HTTPException) as exc_info:
        await routes.chat_in_chatgpt_thread(
            "../../etc/passwd",
            FakeRequest(),
            ChatRequest(messages=[{"role": "user", "content": "hello"}]),
            authenticated=True,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_new_thread_route_returns_openai_payload_with_thread_metadata(monkeypatch):
    service = FakeThreadService()

    async def fake_chat_with_tab(tab_index, request, body, preset_name, authenticated):
        return JSONResponse(
            {
                "id": "chatcmpl-test",
                "choices": [{"message": {"role": "assistant", "content": "answer"}}],
            }
        )

    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)
    monkeypatch.setattr(routes, "chat_with_tab", fake_chat_with_tab)

    response = await routes.create_chatgpt_thread_completion(
        FakeRequest(),
        ChatRequest(messages=[{"role": "user", "content": "hello"}], stream=False),
        authenticated=True,
    )
    payload = json.loads(response.body)

    assert payload["choices"][0]["message"]["content"] == "answer"
    assert payload["chatgpt_thread"]["id"] == THREAD_ID
    assert payload["chatgpt_thread"]["tab_index"] == 5


@pytest.mark.asyncio
async def test_new_thread_route_rejects_streaming_because_id_is_not_known_upfront(monkeypatch):
    monkeypatch.setattr(routes, "_get_thread_service", FakeThreadService)

    with pytest.raises(HTTPException) as exc_info:
        await routes.create_chatgpt_thread_completion(
            FakeRequest(),
            ChatRequest(messages=[{"role": "user", "content": "hello"}], stream=True),
            authenticated=True,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_catgpt_compatible_thread_chat_returns_simple_shape(monkeypatch):
    service = FakeThreadService()

    async def fake_chat_with_tab(tab_index, request, body, preset_name, authenticated):
        return JSONResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "simple answer"}}]
            }
        )

    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)
    monkeypatch.setattr(routes, "chat_with_tab", fake_chat_with_tab)

    result = await routes.chat_in_thread_compat(
        THREAD_ID,
        FakeRequest(),
        routes.ThreadChatRequest(message="hello"),
        authenticated=True,
    )

    assert result["message"] == "simple answer"
    assert result["thread_id"] == THREAD_ID
    assert result["tab_index"] == 4


@pytest.mark.asyncio
async def test_catgpt_compatible_new_thread_returns_generated_id(monkeypatch):
    service = FakeThreadService()

    async def fake_chat_with_tab(tab_index, request, body, preset_name, authenticated):
        return JSONResponse(
            {"choices": [{"message": {"role": "assistant", "content": "created"}}]}
        )

    monkeypatch.setattr(routes, "_get_thread_service", lambda: service)
    monkeypatch.setattr(routes, "chat_with_tab", fake_chat_with_tab)

    result = await routes.create_thread_compat(
        FakeRequest(),
        routes.ThreadChatRequest(message="hello"),
        authenticated=True,
    )

    assert result == {"message": "created", "thread_id": THREAD_ID, "tab_index": 5}


def test_route_helpers_validate_payloads_and_service_errors():
    assert routes._assistant_message({}) == ""
    assert routes._response_headers_without_content_length(
        JSONResponse({"ok": True}, headers={"X-Test": "yes"})
    )["x-test"] == "yes"
    assert routes._translate_service_error(RuntimeError("tab_pool_full")).status_code == 409
    assert (
        routes._translate_service_error(RuntimeError("chatgpt_login_required")).status_code
        == 401
    )
    assert (
        routes._translate_service_error(RuntimeError("chatgpt_thread_not_found")).status_code
        == 404
    )
    assert routes._translate_service_error(RuntimeError("offline")).status_code == 503

    with pytest.raises(HTTPException) as invalid_json:
        routes._json_response_payload(type("Response", (), {"body": b"bad", "status_code": 200})())
    assert invalid_json.value.status_code == 502

    with pytest.raises(HTTPException) as upstream_error:
        routes._json_response_payload(JSONResponse({"detail": "failed"}, status_code=500))
    assert upstream_error.value.status_code == 500


@pytest.mark.asyncio
async def test_list_threads_translates_service_failure(monkeypatch):
    class BrokenService:
        def list_threads(self):
            raise RuntimeError("browser_offline")

    monkeypatch.setattr(routes, "_get_thread_service", BrokenService)
    with pytest.raises(HTTPException) as exc_info:
        await routes.list_chatgpt_threads(authenticated=True)
    assert exc_info.value.status_code == 503

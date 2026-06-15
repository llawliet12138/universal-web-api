"""Thread-aware API routes backed by the logged-in ChatGPT website."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.chat import ChatRequest
from app.api.deps import verify_auth
from app.api.tab_routes import chat_with_tab
from app.core import get_browser
from app.services.chatgpt_threads import (
    CHATGPT_CONTINUE_PRESET,
    ChatGPTThreadService,
    normalize_thread_id,
)


router = APIRouter(tags=["ChatGPT Threads"])


class ThreadChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500_000)
    model: str = Field(default="web-browser", min_length=1, max_length=200)


def _get_thread_service() -> ChatGPTThreadService:
    return ChatGPTThreadService(get_browser(auto_connect=False))


def _validated_thread_id(thread_id: str) -> str:
    try:
        return normalize_thread_id(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ChatGPT thread ID 无效") from exc


def _translate_service_error(exc: RuntimeError) -> HTTPException:
    error = str(exc or "chatgpt_thread_unavailable")
    status_code = {
        "chatgpt_login_required": 401,
        "chatgpt_thread_not_found": 404,
        "tab_pool_full": 409,
    }.get(error, 503)
    return HTTPException(status_code=status_code, detail=error)


def _force_continue_preset(body: ChatRequest) -> ChatRequest:
    return body.model_copy(update={"preset_name": CHATGPT_CONTINUE_PRESET})


def _latest_user_message_only(body: ChatRequest) -> ChatRequest:
    for message in reversed(body.messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        return body.model_copy(update={"messages": [dict(message)]})
    raise HTTPException(status_code=400, detail="已有网页会话需要至少一条 user 消息")


def _json_response_payload(response: JSONResponse) -> Dict[str, Any]:
    try:
        payload = json.loads(bytes(response.body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="浏览器响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="浏览器响应格式无效")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=payload)
    return payload


def _response_headers_without_content_length(response: JSONResponse) -> Dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() != "content-length"
    }


def _assistant_message(payload: Dict[str, Any]) -> str:
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


async def _run_thread_completion(
    *,
    tab_info: Dict[str, Any],
    request: Request,
    body: ChatRequest,
    latest_user_only: bool = False,
):
    tab_index = int(tab_info.get("persistent_index") or 0)
    if tab_index < 1:
        raise HTTPException(status_code=503, detail="ChatGPT 标签页编号无效")
    effective_body = _latest_user_message_only(body) if latest_user_only else body
    return await chat_with_tab(
        tab_index=tab_index,
        request=request,
        body=_force_continue_preset(effective_body),
        preset_name=CHATGPT_CONTINUE_PRESET,
        authenticated=True,
    )


@router.get("/api/chatgpt/threads")
@router.get("/threads", include_in_schema=False)
async def list_chatgpt_threads(authenticated: bool = Depends(verify_auth)):
    service = _get_thread_service()
    try:
        threads = await asyncio.to_thread(service.list_threads)
    except RuntimeError as exc:
        raise _translate_service_error(exc) from exc
    return {"threads": threads, "count": len(threads)}


@router.post("/api/chatgpt/threads/new/v1/chat/completions")
async def create_chatgpt_thread_completion(
    request: Request,
    body: ChatRequest,
    authenticated: bool = Depends(verify_auth),
):
    if body.stream:
        raise HTTPException(
            status_code=400,
            detail="新建网页会话暂不支持 stream=true；首轮完成后才能取得 thread ID",
        )

    service = _get_thread_service()
    try:
        tab_info = await asyncio.to_thread(service.create_new_thread_tab)
        response = await _run_thread_completion(
            tab_info=tab_info,
            request=request,
            body=body,
        )
    except RuntimeError as exc:
        raise _translate_service_error(exc) from exc

    if not isinstance(response, JSONResponse):
        raise HTTPException(status_code=502, detail="新建网页会话返回了非 JSON 响应")
    payload = _json_response_payload(response)
    tab_index = int(tab_info.get("persistent_index") or 0)
    thread = await asyncio.to_thread(service.get_thread_for_tab, tab_index)
    if thread is None:
        raise HTTPException(status_code=502, detail="ChatGPT 首轮完成后未生成 thread ID")
    enriched_payload = {**payload, "chatgpt_thread": thread}
    return JSONResponse(
        content=enriched_payload,
        status_code=response.status_code,
        headers=_response_headers_without_content_length(response),
    )


@router.post("/api/chatgpt/threads/{thread_id}/v1/chat/completions")
async def chat_in_chatgpt_thread(
    thread_id: str,
    request: Request,
    body: ChatRequest,
    authenticated: bool = Depends(verify_auth),
):
    canonical_id = _validated_thread_id(thread_id)
    effective_body = _latest_user_message_only(body)
    service = _get_thread_service()
    try:
        tab_info = await asyncio.to_thread(service.ensure_thread_tab, canonical_id)
        return await _run_thread_completion(
            tab_info=tab_info,
            request=request,
            body=effective_body,
        )
    except RuntimeError as exc:
        raise _translate_service_error(exc) from exc


@router.post("/thread/{thread_id}/chat", include_in_schema=False)
async def chat_in_thread_compat(
    thread_id: str,
    request: Request,
    body: ThreadChatRequest,
    authenticated: bool = Depends(verify_auth),
):
    canonical_id = _validated_thread_id(thread_id)
    service = _get_thread_service()
    try:
        tab_info = await asyncio.to_thread(service.ensure_thread_tab, canonical_id)
        response = await _run_thread_completion(
            tab_info=tab_info,
            request=request,
            body=ChatRequest(
                model=body.model,
                messages=[{"role": "user", "content": body.message}],
                stream=False,
            ),
            latest_user_only=True,
        )
    except RuntimeError as exc:
        raise _translate_service_error(exc) from exc
    if not isinstance(response, JSONResponse):
        raise HTTPException(status_code=502, detail="浏览器返回了非 JSON 响应")
    payload = _json_response_payload(response)
    return {
        "message": _assistant_message(payload),
        "thread_id": canonical_id,
        "tab_index": int(tab_info.get("persistent_index") or 0),
    }


@router.post("/thread/new", include_in_schema=False)
async def create_thread_compat(
    request: Request,
    body: ThreadChatRequest,
    authenticated: bool = Depends(verify_auth),
):
    service = _get_thread_service()
    try:
        tab_info = await asyncio.to_thread(service.create_new_thread_tab)
        response = await _run_thread_completion(
            tab_info=tab_info,
            request=request,
            body=ChatRequest(
                model=body.model,
                messages=[{"role": "user", "content": body.message}],
                stream=False,
            ),
        )
    except RuntimeError as exc:
        raise _translate_service_error(exc) from exc
    if not isinstance(response, JSONResponse):
        raise HTTPException(status_code=502, detail="浏览器返回了非 JSON 响应")
    payload = _json_response_payload(response)
    tab_index = int(tab_info.get("persistent_index") or 0)
    thread = await asyncio.to_thread(service.get_thread_for_tab, tab_index)
    if thread is None:
        raise HTTPException(status_code=502, detail="ChatGPT 首轮完成后未生成 thread ID")
    return {
        "message": _assistant_message(payload),
        "thread_id": thread["id"],
        "tab_index": tab_index,
    }

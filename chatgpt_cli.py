#!/usr/bin/env python3
"""Interactive terminal client for Universal Web API ChatGPT threads."""

from __future__ import annotations

import argparse
import builtins
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


THREAD_PAGE_SIZE = 10
_ANSI_SKY_BLUE = "\033[38;2;56;189;248m"
_ANSI_RESET = "\033[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_THREAD_PAGE_RENDERED_LINES = 0


def _http_error_message(exc: HTTPError) -> str:
    raw_detail = exc.read().decode("utf-8", errors="replace").strip()
    detail = raw_detail
    if raw_detail:
        try:
            payload = json.loads(raw_detail)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            detail_value = payload.get("detail")
            if isinstance(detail_value, str):
                detail = detail_value.strip()

    if detail == "chatgpt_login_required":
        return "受控浏览器尚未登录；请在受控浏览器中登录 ChatGPT 后重试"
    if exc.code == 503:
        return (
            "本地服务暂不可用；请确认另一个终端中的 python3 start.py 仍在运行，"
            "并确认受控浏览器已启动并登录 ChatGPT"
        )
    if detail:
        return f"HTTP {exc.code}: {detail}"
    return f"HTTP {exc.code}: {exc.reason or '请求失败'}"


class ChatGPTWebClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 600,
        opener: Callable[..., Any] = urlopen,
        bridge_id: str = "",
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("base URL must point to localhost")
        self.token = str(token or "").strip()
        self.timeout = float(timeout)
        self.opener = opener
        self.bridge_id = str(bridge_id or uuid.uuid4()).strip()

    def list_threads(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/threads")
        threads = payload.get("threads", [])
        return threads if isinstance(threads, list) else []

    def new_thread(self, message: str, model: str = "web-browser") -> Dict[str, Any]:
        return self._request(
            "POST",
            "/thread/new",
            {"message": message, "model": model},
        )

    def chat(
        self,
        thread_id: str,
        message: str,
        model: str = "web-browser",
    ) -> Dict[str, Any]:
        safe_thread_id = quote(str(thread_id or "").strip(), safe="")
        return self._request(
            "POST",
            f"/thread/{safe_thread_id}/chat",
            {"message": message, "model": model},
        )

    def stream_chat(
        self,
        thread_id: str,
        message: str,
        model: str = "web-browser",
    ):
        safe_thread_id = quote(str(thread_id or "").strip(), safe="")
        request = self._build_request(
            "POST",
            f"/api/chatgpt/threads/{safe_thread_id}/v1/chat/completions",
            {
                "model": model,
                "stream": True,
                "messages": [{"role": "user", "content": message}],
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                yield from _iter_openai_stream_content(response)
        except HTTPError as exc:
            raise RuntimeError(_http_error_message(exc)) from exc
        except URLError as exc:
            raise RuntimeError(
                "无法连接本地服务；请先在另一个终端运行 python3 start.py，"
                f"并保持该终端运行（原因: {exc.reason}）"
            ) from exc

    def activate(self, thread_id: Optional[str]) -> Dict[str, Any]:
        safe_bridge_id = quote(self.bridge_id, safe="")
        return self._request(
            "POST",
            f"/api/chatgpt/bridge/{safe_bridge_id}/activate",
            {"thread_id": str(thread_id).strip() if thread_id else None},
        )

    def release(self) -> Dict[str, Any]:
        safe_bridge_id = quote(self.bridge_id, safe="")
        return self._request("DELETE", f"/api/chatgpt/bridge/{safe_bridge_id}")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request = self._build_request(method, path, payload)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(_http_error_message(exc)) from exc
        except URLError as exc:
            raise RuntimeError(
                "无法连接本地服务；请先在另一个终端运行 python3 start.py，"
                f"并保持该终端运行（原因: {exc.reason}）"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("本地服务返回了无效 JSON") from exc

        if not isinstance(decoded, dict):
            raise RuntimeError("本地服务返回格式无效")
        return decoded

    def _build_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Request:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers["X-ChatGPT-Bridge-ID"] = self.bridge_id

        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        return request


def _decode_response_chunk(chunk: Any) -> str:
    if not chunk:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _read_response_text(response: Any, size: int = 4096) -> str:
    return _decode_response_chunk(response.read(size))


def _iter_sse_payloads_from_text(text: str):
    for frame in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        data_lines = []
        for raw_line in frame.splitlines():
            line = raw_line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        data_text = "\n".join(data_lines)
        if not data_text or data_text == "[DONE]":
            continue
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _stream_content_from_payload(payload: Dict[str, Any]) -> str:
    try:
        choice = payload["choices"][0]
        delta = choice.get("delta") if isinstance(choice, dict) else {}
        if isinstance(delta, dict):
            content = delta.get("content")
            return content if isinstance(content, str) else ""
    except (KeyError, IndexError, TypeError):
        return ""
    return ""


def _iter_openai_stream_content(response: Any):
    buffer = ""
    while True:
        if hasattr(response, "readline"):
            chunk = _decode_response_chunk(response.readline())
        else:
            chunk = _read_response_text(response)
        if not chunk:
            break
        buffer += chunk
        normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
        parts = normalized.split("\n\n")
        buffer = parts.pop() if parts else ""
        for payload in _iter_sse_payloads_from_text("\n\n".join(parts) + "\n\n"):
            content = _stream_content_from_payload(payload)
            if content:
                yield content
    if buffer.strip():
        for payload in _iter_sse_payloads_from_text(buffer + "\n\n"):
            content = _stream_content_from_payload(payload)
            if content:
                yield content


def _display_width(text: str) -> int:
    width = 0
    for char in _ANSI_RE.sub("", str(text or "")):
        if unicodedata.combining(char):
            continue
        if unicodedata.category(char)[0] == "C":
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    raw = " ".join(str(text or "").split())
    if max_width <= 1 or _display_width(raw) <= max_width:
        return raw

    result = ""
    used = 0
    for char in raw:
        char_width = _display_width(char)
        if used + char_width > max_width - 1:
            break
        result += char
        used += char_width
    return f"{result}…"


def _pad_display(text: str, width: int) -> str:
    return f"{text}{' ' * max(0, width - _display_width(text))}"


def _color_pinned(text: str) -> str:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"{_ANSI_SKY_BLUE}{text}{_ANSI_RESET}"


def _thread_title(item: Dict[str, Any]) -> str:
    return " ".join(str((item or {}).get("title") or "Untitled").split()) or "Untitled"


def _thread_is_pinned(item: Dict[str, Any]) -> bool:
    return bool(
        (item or {}).get("is_pinned")
        or (item or {}).get("pinned")
        or (item or {}).get("isPinned")
    )


def _thread_key_for_local_index(local_index: int) -> str:
    if local_index < 0 or local_index > 9:
        return ""
    return str(local_index)


def _thread_index_from_key(key: str, page_count: int) -> Optional[int]:
    normalized = str(key or "").strip().lower()
    if normalized not in {str(value) for value in range(10)}:
        return None
    index = int(normalized)
    if index < 0 or index >= page_count:
        return None
    return index


def _format_thread_cell(item: Dict[str, Any], local_index: int, width: int) -> str:
    prefix = f"{_thread_key_for_local_index(local_index):>2}. "
    title_budget = max(8, width - _display_width(prefix))
    title = _truncate_display(_thread_title(item), title_budget)
    if _thread_is_pinned(item):
        title = _color_pinned(title)
    plain_cell = f"{prefix}{title}"
    return _pad_display(plain_cell, width)


def _thread_grid_columns(page_items: List[Dict[str, Any]], terminal_width: int) -> int:
    if len(page_items) <= 1:
        return 1
    if terminal_width >= 112:
        return min(3, len(page_items))
    if terminal_width >= 72:
        return min(2, len(page_items))
    return 1


def _clear_previous_thread_page() -> None:
    global _THREAD_PAGE_RENDERED_LINES
    if _THREAD_PAGE_RENDERED_LINES <= 0 or not sys.stdout.isatty():
        _THREAD_PAGE_RENDERED_LINES = 0
        return
    sys.stdout.write(f"\033[{_THREAD_PAGE_RENDERED_LINES + 1}A\033[J")
    sys.stdout.flush()
    _THREAD_PAGE_RENDERED_LINES = 0


def _print_thread_page(
    threads: List[Dict[str, Any]],
    *,
    offset: int = 0,
    page_size: int = THREAD_PAGE_SIZE,
    redraw: bool = False,
) -> None:
    global _THREAD_PAGE_RENDERED_LINES
    if not threads:
        print("没有发现 ChatGPT 网页会话。")
        _THREAD_PAGE_RENDERED_LINES = 1
        return

    if redraw:
        _clear_previous_thread_page()

    safe_offset = max(0, min(int(offset or 0), max(0, len(threads) - 1)))
    page_items = threads[safe_offset:safe_offset + max(1, int(page_size or THREAD_PAGE_SIZE))]
    end = safe_offset + len(page_items)
    terminal_width = shutil.get_terminal_size((100, 24)).columns
    columns = _thread_grid_columns(page_items, terminal_width)
    gutter = 3
    cell_width = max(20, (terminal_width - gutter * (columns - 1)) // columns)
    rows = (len(page_items) + columns - 1) // columns

    rendered_lines = 0
    print(f"\n会话 {safe_offset + 1}-{end} / {len(threads)}")
    rendered_lines += 2
    for row in range(rows):
        cells = []
        for column in range(columns):
            local_index = row + column * rows
            if local_index >= len(page_items):
                continue
            cells.append(_format_thread_cell(page_items[local_index], local_index, cell_width))
        print((" " * gutter).join(cells).rstrip())
        rendered_lines += 1

    hints = ["按 0-9 选择", "n 新建"]
    if end < len(threads):
        hints.append("m 后 10 个")
    if safe_offset > 0:
        hints.append("p 前 10 个")
    print("；".join(hints))
    rendered_lines += 1
    _THREAD_PAGE_RENDERED_LINES = rendered_lines


def _print_threads(threads: List[Dict[str, Any]]) -> None:
    _print_thread_page(threads)


def _choose_thread(client: ChatGPTWebClient) -> Optional[str]:
    selected = _choose_thread_item(client)
    return str((selected or {}).get("id") or "") or None


def _read_thread_picker_key(prompt: str) -> str:
    if not sys.stdin.isatty():
        return builtins.input(prompt).strip().lower()

    if os.name == "nt":
        import msvcrt

        sys.stdout.write(prompt)
        sys.stdout.flush()
        key = msvcrt.getwch()
        print()
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write(prompt)
            sys.stdout.flush()
            key = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        print()

    if key == "\x03":
        raise KeyboardInterrupt
    if key == "\x04":
        raise EOFError
    return str(key or "").strip().lower()


def _choose_thread_item(client: ChatGPTWebClient) -> Optional[Dict[str, Any]]:
    global _THREAD_PAGE_RENDERED_LINES
    threads = client.list_threads()
    if not threads:
        _print_threads(threads)
        return None
    offset = 0
    while True:
        _print_thread_page(threads, offset=offset, redraw=_THREAD_PAGE_RENDERED_LINES > 0)
        page_end = min(offset + THREAD_PAGE_SIZE, len(threads))
        page_count = max(0, page_end - offset)
        choice = _read_thread_picker_key("")
        if choice in {"", "n", "new"}:
            _THREAD_PAGE_RENDERED_LINES = 0
            return None
        if choice in {"m", "more", "next"}:
            if page_end >= len(threads):
                print("没有更多已加载会话。")
            else:
                offset = page_end
            continue
        if choice in {"p", "prev", "previous", "back"}:
            offset = max(0, offset - THREAD_PAGE_SIZE)
            continue
        local_index = _thread_index_from_key(choice, page_count)
        if local_index is None:
            raise RuntimeError("会话编号无效")
        selected_index = offset + local_index
        _THREAD_PAGE_RENDERED_LINES = 0
        return threads[selected_index]


def _conversation_label(thread_id: Optional[str], title: str = "") -> str:
    clean_title = " ".join(str(title or "").split())
    if clean_title:
        return clean_title
    return thread_id[:8] if thread_id else "new"


def _print_session_banner(thread_id: Optional[str], title: str = "") -> None:
    label = _conversation_label(thread_id, title)
    print("\n================ ChatGPT CLI 会话开始 ================")
    print(f"当前会话: {label}")
    print("命令: /threads 切换，/new 新建，/exit 退出")
    print("=====================================================")


def _print_switch_banner(thread_id: Optional[str], title: str = "") -> None:
    print(f"\n---------------- 已切换会话: {_conversation_label(thread_id, title)} ----------------")


def run_interactive(
    client: ChatGPTWebClient,
    thread_id: Optional[str],
    model: str,
    *,
    thread_title: str = "",
) -> int:
    active_thread = thread_id
    active_title = thread_title
    _print_session_banner(active_thread, active_title)
    client.activate(active_thread)
    try:
        while True:
            prompt = f"[{active_thread[:8] if active_thread else 'new'}] > "
            try:
                message = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                return 0
            if message == "/new":
                active_thread = None
                active_title = ""
                client.activate(None)
                _print_switch_banner(active_thread, active_title)
                continue
            if message == "/threads":
                selected = _choose_thread_item(client)
                active_thread = str((selected or {}).get("id") or "") or None
                active_title = _thread_title(selected) if selected else ""
                client.activate(active_thread)
                _print_switch_banner(active_thread, active_title)
                continue

            if active_thread:
                for part in client.stream_chat(active_thread, message, model=model):
                    print(part, end="", flush=True)
                print()
            else:
                result = client.new_thread(message, model=model)
                active_thread = str(result.get("thread_id") or "") or None
                active_title = ""
                if active_thread:
                    client.activate(active_thread)
                    _print_switch_banner(active_thread, active_title)
                print(result.get("message") or "")
    finally:
        try:
            client.release()
        except RuntimeError as exc:
            print(f"警告: 无法释放后台桥接标签页: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chat with the logged-in ChatGPT website")
    parser.add_argument("--base-url", default="http://127.0.0.1:8199")
    parser.add_argument("--token", default="", help="AUTH_TOKEN when authentication is enabled")
    parser.add_argument("--thread", default="", help="ChatGPT conversation UUID")
    parser.add_argument("--new", action="store_true", help="Start with a new web conversation")
    parser.add_argument("--model", default="web-browser")
    parser.add_argument("--timeout", type=float, default=600)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = ChatGPTWebClient(
            args.base_url,
            token=args.token,
            timeout=args.timeout,
        )
        selected_title = ""
        if args.new:
            thread_id = None
        elif args.thread.strip():
            thread_id = args.thread.strip()
        else:
            selected = _choose_thread_item(client)
            thread_id = str((selected or {}).get("id") or "") or None
            selected_title = _thread_title(selected) if selected else ""
        return run_interactive(client, thread_id, args.model, thread_title=selected_title)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

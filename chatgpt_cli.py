#!/usr/bin/env python3
"""Interactive terminal client for Universal Web API ChatGPT threads."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ChatGPTWebClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 600,
        opener: Callable[..., Any] = urlopen,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("base URL must point to localhost")
        self.token = str(token or "").strip()
        self.timeout = float(timeout)
        self.opener = opener

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

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"无法连接本地服务: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("本地服务返回了无效 JSON") from exc

        if not isinstance(decoded, dict):
            raise RuntimeError("本地服务返回格式无效")
        return decoded


def _print_threads(threads: List[Dict[str, Any]]) -> None:
    if not threads:
        print("没有发现 ChatGPT 网页会话。")
        return
    for index, item in enumerate(threads, start=1):
        marker = "open" if item.get("is_open") else "closed"
        print(f"{index:>2}. [{marker}] {item.get('title') or 'Untitled'}")
        print(f"    {item.get('id') or ''}")


def _choose_thread(client: ChatGPTWebClient) -> Optional[str]:
    threads = client.list_threads()
    _print_threads(threads)
    if not threads:
        return None
    choice = input("选择编号，或输入 n 新建会话: ").strip().lower()
    if choice in {"", "n", "new"}:
        return None
    try:
        selected = threads[int(choice) - 1]
    except (ValueError, IndexError):
        raise RuntimeError("会话编号无效")
    return str(selected.get("id") or "") or None


def run_interactive(client: ChatGPTWebClient, thread_id: Optional[str], model: str) -> int:
    active_thread = thread_id
    print("输入 /threads 切换会话，/new 新建会话，/exit 退出。")
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
            continue
        if message == "/threads":
            active_thread = _choose_thread(client)
            continue

        if active_thread:
            result = client.chat(active_thread, message, model=model)
        else:
            result = client.new_thread(message, model=model)
            active_thread = str(result.get("thread_id") or "") or None
        print(result.get("message") or "")


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
        thread_id = None if args.new else (args.thread.strip() or _choose_thread(client))
        return run_interactive(client, thread_id, args.model)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

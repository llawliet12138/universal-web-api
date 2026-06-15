"""ChatGPT web conversation discovery and tab lifecycle helpers."""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlsplit


CHATGPT_DOMAIN = "chatgpt.com"
CHATGPT_BASE_URL = "https://chatgpt.com/"
CHATGPT_CONTINUE_PRESET = "继续当前会话"
_THREAD_PATH_RE = re.compile(
    r"^/c/(?P<thread_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$"
)

_SIDEBAR_THREAD_SCRIPT = r"""
return Array.from(document.querySelectorAll('a[href^="/c/"]'))
    .map((anchor) => ({
        href: anchor.getAttribute('href') || '',
        title: (
            anchor.innerText ||
            anchor.textContent ||
            anchor.getAttribute('title') ||
            anchor.getAttribute('aria-label') ||
            ''
        ).trim()
    }));
"""

_LOGIN_REQUIRED_SCRIPT = r"""
return Boolean(document.querySelector(
    '[data-testid="login-button"], [data-testid="signup-button"]'
));
"""

_THREAD_OPEN_LOCK = threading.Lock()


def normalize_thread_id(value: str) -> str:
    """Return a canonical ChatGPT UUID or raise ValueError."""
    raw = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("invalid_chatgpt_thread_id") from exc
    canonical = str(parsed)
    if raw.lower() != canonical:
        raise ValueError("invalid_chatgpt_thread_id")
    return canonical


def thread_url(thread_id: str) -> str:
    return f"{CHATGPT_BASE_URL}c/{normalize_thread_id(thread_id)}"


def extract_thread_id(url: str) -> Optional[str]:
    """Extract a canonical thread UUID from a chatgpt.com conversation URL."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != CHATGPT_DOMAIN:
        return None
    match = _THREAD_PATH_RE.fullmatch(parsed.path or "")
    if not match:
        return None
    try:
        return normalize_thread_id(match.group("thread_id"))
    except ValueError:
        return None


def normalize_thread_candidates(items: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Normalize sidebar links and discard invalid or duplicate entries."""
    threads: Dict[str, Dict[str, str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        absolute_url = urljoin(CHATGPT_BASE_URL, href)
        thread_id = extract_thread_id(absolute_url)
        if not thread_id or thread_id in threads:
            continue
        title = " ".join(str(item.get("title") or "").split())
        threads[thread_id] = {
            "id": thread_id,
            "title": title or "Untitled conversation",
            "url": thread_url(thread_id),
        }
    return list(threads.values())


class ChatGPTThreadService:
    """Manage ChatGPT conversation tabs without owning response execution."""

    def __init__(self, browser: Any):
        self.browser = browser

    @property
    def tab_pool(self):
        pool = getattr(self.browser, "tab_pool", None)
        if pool is None:
            raise RuntimeError("tab_pool_unavailable")
        return pool

    def list_threads(self) -> List[Dict[str, Any]]:
        tabs = self.tab_pool.get_tabs_with_index()
        chatgpt_tabs = [item for item in tabs if self._is_chatgpt_tab(item)]
        merged: Dict[str, Dict[str, Any]] = {}

        for item in chatgpt_tabs:
            current_thread_id = extract_thread_id(item.get("url"))
            if not current_thread_id:
                continue
            merged[current_thread_id] = {
                "id": current_thread_id,
                "title": "Open conversation",
                "url": thread_url(current_thread_id),
                "tab_index": int(item.get("persistent_index") or 0) or None,
                "is_open": True,
            }

        sidebar_items = self._read_sidebar_threads(chatgpt_tabs)
        for item in normalize_thread_candidates(sidebar_items):
            existing = merged.get(item["id"])
            merged[item["id"]] = {
                **item,
                "tab_index": existing.get("tab_index") if existing else None,
                "is_open": bool(existing),
            }

        return list(merged.values())

    def ensure_thread_tab(self, thread_id: str) -> Dict[str, Any]:
        canonical_id = normalize_thread_id(thread_id)
        with _THREAD_OPEN_LOCK:
            for item in self.tab_pool.get_tabs_with_index():
                if extract_thread_id(item.get("url")) == canonical_id:
                    tab_info = dict(item)
                    self._require_signed_in(tab_info, expected_thread_id=canonical_id)
                    return tab_info

            result = self.tab_pool.create_shared_url_tab(
                thread_url(canonical_id),
                expected_domain=CHATGPT_DOMAIN,
            )
            if not result.get("ok") or not isinstance(result.get("tab"), dict):
                error = str(result.get("error") or "create_chatgpt_thread_tab_failed")
                raise RuntimeError(error)
            tab_info = dict(result["tab"])
            self._require_signed_in(tab_info, expected_thread_id=canonical_id)
            return tab_info

    def create_new_thread_tab(self) -> Dict[str, Any]:
        result = self.tab_pool.create_shared_url_tab(
            CHATGPT_BASE_URL,
            expected_domain=CHATGPT_DOMAIN,
        )
        if not result.get("ok") or not isinstance(result.get("tab"), dict):
            error = str(result.get("error") or "create_chatgpt_thread_tab_failed")
            raise RuntimeError(error)
        tab_info = dict(result["tab"])
        self._require_signed_in(tab_info)
        return tab_info

    def get_thread_for_tab(self, tab_index: int) -> Optional[Dict[str, Any]]:
        target_index = int(tab_index or 0)
        for item in self.tab_pool.get_tabs_with_index():
            if int(item.get("persistent_index") or 0) != target_index:
                continue
            current_thread_id = extract_thread_id(item.get("url"))
            if not current_thread_id:
                return None
            return {
                "id": current_thread_id,
                "url": thread_url(current_thread_id),
                "tab_index": target_index,
            }
        return None

    @staticmethod
    def _is_chatgpt_tab(item: Dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            return False
        domain = str(item.get("current_domain") or item.get("route_domain") or "").lower()
        if domain == CHATGPT_DOMAIN:
            return True
        try:
            return (urlsplit(str(item.get("url") or "")).hostname or "").lower() == CHATGPT_DOMAIN
        except ValueError:
            return False

    def _read_sidebar_threads(self, chatgpt_tabs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = sorted(
            chatgpt_tabs,
            key=lambda item: (
                str(item.get("status") or "").lower() != "idle",
                int(item.get("persistent_index") or 0),
            ),
        )
        for item in candidates:
            tab_index = int(item.get("persistent_index") or 0)
            if tab_index < 1:
                continue
            task_id = f"chatgpt-thread-list-{uuid.uuid4().hex}"
            session = self.tab_pool.acquire_by_index(tab_index, task_id, timeout=2.0)
            if session is None:
                continue
            try:
                raw_items = session.tab.run_js(_SIDEBAR_THREAD_SCRIPT)
                return raw_items if isinstance(raw_items, list) else []
            finally:
                self.tab_pool.release(
                    session.id,
                    check_triggers=False,
                    rollback_request_count=True,
                    expected_task_id=task_id,
                )
        return []

    def _require_signed_in(
        self,
        tab_info: Dict[str, Any],
        *,
        expected_thread_id: Optional[str] = None,
    ) -> None:
        tab_index = int(tab_info.get("persistent_index") or 0)
        if tab_index < 1:
            raise RuntimeError("chatgpt_tab_unavailable")

        task_id = f"chatgpt-login-check-{uuid.uuid4().hex}"
        session = self.tab_pool.acquire_by_index(tab_index, task_id, timeout=10.0)
        if session is None:
            raise RuntimeError("chatgpt_tab_unavailable")
        try:
            if bool(session.tab.run_js(_LOGIN_REQUIRED_SCRIPT)):
                raise RuntimeError("chatgpt_login_required")
            if expected_thread_id:
                actual_thread_id = extract_thread_id(getattr(session.tab, "url", ""))
                if actual_thread_id != expected_thread_id:
                    raise RuntimeError("chatgpt_thread_not_found")
        finally:
            self.tab_pool.release(
                session.id,
                check_triggers=False,
                rollback_request_count=True,
                expected_task_id=task_id,
            )

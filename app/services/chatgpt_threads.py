"""ChatGPT web conversation discovery and tab lifecycle helpers."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
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
    .map((anchor) => {
        const title = (
            anchor.innerText ||
            anchor.textContent ||
            anchor.getAttribute('title') ||
            anchor.getAttribute('aria-label') ||
            ''
        ).trim();
        const nearby = [];
        let node = anchor;
        for (let i = 0; i < 5 && node; i += 1) {
            nearby.push(
                node.getAttribute?.('aria-label') || '',
                node.getAttribute?.('title') || '',
                node.getAttribute?.('data-testid') || '',
                node.className || ''
            );
            node = node.parentElement;
        }
        const haystack = nearby.join(' ').toLowerCase();
        return {
            href: anchor.getAttribute('href') || '',
            title,
            is_pinned: /pinned|pin-|置顶|已固定|固定/.test(haystack)
        };
    });
"""

_LOGIN_REQUIRED_SCRIPT = r"""
return Boolean(document.querySelector(
    '[data-testid="login-button"], [data-testid="signup-button"]'
));
"""

_THREAD_OPEN_LOCK = threading.Lock()


def normalize_bridge_id(value: str) -> str:
    raw = str(value or "").strip()
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("invalid_chatgpt_bridge_id") from exc


@dataclass
class _BridgeLease:
    bridge_id: str
    pool: Any
    persistent_index: int
    current_url: str
    thread_id: Optional[str]
    last_activity: float
    active_requests: int = 0


class ChatGPTBridgeRegistry:
    """Own one hidden ChatGPT worker tab per local client bridge ID."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 1800.0,
        cleanup_interval: float = 60.0,
        start_cleaner: bool = True,
    ):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.cleanup_interval = max(1.0, float(cleanup_interval))
        self._condition = threading.Condition(threading.RLock())
        self._leases: Dict[str, _BridgeLease] = {}
        self._stop_event = threading.Event()
        self._cleaner_thread: Optional[threading.Thread] = None
        if start_cleaner:
            self._cleaner_thread = threading.Thread(
                target=self._cleanup_loop,
                name="chatgpt-bridge-cleaner",
                daemon=True,
            )
            self._cleaner_thread.start()

    def _wait_until_idle(self, bridge_id: str, timeout: float = 600.0) -> None:
        deadline = time.time() + max(1.0, float(timeout))
        while True:
            lease = self._leases.get(bridge_id)
            if lease is None or lease.active_requests <= 0:
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError("chatgpt_bridge_busy")
            self._condition.wait(timeout=min(remaining, 1.0))

    def _ensure_tab_locked(
        self,
        pool: Any,
        bridge_id: str,
        target_url: str,
        thread_id: Optional[str],
    ) -> Dict[str, Any]:
        lease = self._leases.get(bridge_id)
        if lease is not None and lease.pool is not pool:
            self._leases.pop(bridge_id, None)
            lease = None

        existing = pool.get_owned_tab_info(bridge_id) if lease is not None else None
        if lease is not None and existing is None:
            self._leases.pop(bridge_id, None)
            try:
                pool.close_owned_tab(bridge_id)
            except Exception:
                pass
            lease = None

        if lease is None:
            result = pool.create_shared_url_tab(
                target_url,
                expected_domain=CHATGPT_DOMAIN,
                background=True,
                new_window=False,
                owner_id=bridge_id,
            )
            if not result.get("ok") or not isinstance(result.get("tab"), dict):
                raise RuntimeError(str(result.get("error") or "create_chatgpt_bridge_tab_failed"))
            tab_info = dict(result["tab"])
            persistent_index = int(tab_info.get("persistent_index") or 0)
            if persistent_index < 1:
                raise RuntimeError("chatgpt_tab_unavailable")
            lease = _BridgeLease(
                bridge_id=bridge_id,
                pool=pool,
                persistent_index=persistent_index,
                current_url=target_url,
                thread_id=thread_id,
                last_activity=time.time(),
            )
            self._leases[bridge_id] = lease
            return tab_info

        tab_info = dict(existing)
        if lease.current_url != target_url:
            result = pool.navigate_owned_tab(bridge_id, target_url, timeout=600.0)
            if not result.get("ok") or not isinstance(result.get("tab"), dict):
                raise RuntimeError(str(result.get("error") or "chatgpt_bridge_navigation_failed"))
            tab_info = dict(result["tab"])
        lease.persistent_index = int(tab_info.get("persistent_index") or lease.persistent_index)
        lease.current_url = target_url
        lease.thread_id = thread_id
        lease.last_activity = time.time()
        return tab_info

    def activate(
        self,
        pool: Any,
        bridge_id: str,
        target_url: str,
        thread_id: Optional[str],
    ) -> Dict[str, Any]:
        canonical_id = normalize_bridge_id(bridge_id)
        with self._condition:
            self._wait_until_idle(canonical_id)
            return self._ensure_tab_locked(pool, canonical_id, target_url, thread_id)

    def begin_request(
        self,
        pool: Any,
        bridge_id: str,
        target_url: str,
        thread_id: Optional[str],
    ) -> Dict[str, Any]:
        canonical_id = normalize_bridge_id(bridge_id)
        with self._condition:
            self._wait_until_idle(canonical_id)
            tab_info = self._ensure_tab_locked(pool, canonical_id, target_url, thread_id)
            lease = self._leases[canonical_id]
            lease.active_requests += 1
            lease.last_activity = time.time()
            return tab_info

    def end_request(self, bridge_id: str) -> None:
        canonical_id = normalize_bridge_id(bridge_id)
        with self._condition:
            lease = self._leases.get(canonical_id)
            if lease is not None:
                lease.active_requests = max(0, lease.active_requests - 1)
                lease.last_activity = time.time()
            self._condition.notify_all()

    def release(self, pool: Any, bridge_id: str) -> bool:
        canonical_id = normalize_bridge_id(bridge_id)
        with self._condition:
            self._wait_until_idle(canonical_id)
            lease = self._leases.pop(canonical_id, None)
        if lease is None:
            return False
        result = pool.close_owned_tab(canonical_id)
        if not result.get("ok"):
            with self._condition:
                self._leases[canonical_id] = lease
            raise RuntimeError(str(result.get("error") or "chatgpt_bridge_close_failed"))
        return bool(result.get("closed"))

    def cleanup_expired(self, pool: Any = None) -> int:
        now = time.time()
        expired: List[_BridgeLease] = []
        with self._condition:
            for bridge_id, lease in list(self._leases.items()):
                if pool is not None and lease.pool is not pool:
                    continue
                if lease.active_requests > 0 or now - lease.last_activity < self.ttl_seconds:
                    continue
                expired.append(self._leases.pop(bridge_id))
        for lease in expired:
            try:
                result = lease.pool.close_owned_tab(lease.bridge_id)
                if not result.get("ok"):
                    with self._condition:
                        lease.last_activity = time.time()
                        self._leases[lease.bridge_id] = lease
            except Exception:
                with self._condition:
                    lease.last_activity = time.time()
                    self._leases[lease.bridge_id] = lease
        return len(expired)

    def shutdown(self) -> None:
        self._stop_event.set()
        thread = self._cleaner_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._condition:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            try:
                lease.pool.close_owned_tab(lease.bridge_id)
            except Exception:
                pass

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self.cleanup_interval):
            self.cleanup_expired()


try:
    _BRIDGE_TTL_SECONDS = float(os.getenv("CHATGPT_BRIDGE_TTL_SEC", "1800"))
except ValueError:
    _BRIDGE_TTL_SECONDS = 1800.0

chatgpt_bridge_registry = ChatGPTBridgeRegistry(ttl_seconds=_BRIDGE_TTL_SECONDS)


def shutdown_chatgpt_bridges() -> None:
    chatgpt_bridge_registry.shutdown()


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


def normalize_thread_candidates(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize sidebar links and discard invalid or duplicate entries."""
    threads: Dict[str, Dict[str, Any]] = {}
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
            "is_pinned": bool(item.get("is_pinned") or item.get("pinned")),
        }
    return list(threads.values())


class ChatGPTThreadService:
    """Manage ChatGPT conversation tabs without owning response execution."""

    def __init__(
        self,
        browser: Any,
        *,
        bridge_registry: Optional[ChatGPTBridgeRegistry] = None,
    ):
        self.browser = browser
        self.bridge_registry = bridge_registry or chatgpt_bridge_registry

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
                "is_pinned": False,
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
                background=True,
                new_window=False,
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
            background=True,
            new_window=False,
        )
        if not result.get("ok") or not isinstance(result.get("tab"), dict):
            error = str(result.get("error") or "create_chatgpt_thread_tab_failed")
            raise RuntimeError(error)
        tab_info = dict(result["tab"])
        self._require_signed_in(tab_info)
        return tab_info

    def activate_bridge(
        self,
        bridge_id: str,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_thread_id = normalize_thread_id(thread_id) if thread_id else None
        target_url = thread_url(canonical_thread_id) if canonical_thread_id else CHATGPT_BASE_URL
        tab_info = self.bridge_registry.begin_request(
            self.tab_pool,
            bridge_id,
            target_url,
            canonical_thread_id,
        )
        try:
            self._require_signed_in(tab_info, expected_thread_id=canonical_thread_id)
            return tab_info
        finally:
            self.bridge_registry.end_request(bridge_id)

    def begin_bridge_request(
        self,
        bridge_id: str,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_thread_id = normalize_thread_id(thread_id) if thread_id else None
        target_url = thread_url(canonical_thread_id) if canonical_thread_id else CHATGPT_BASE_URL
        tab_info = self.bridge_registry.begin_request(
            self.tab_pool,
            bridge_id,
            target_url,
            canonical_thread_id,
        )
        try:
            self._require_signed_in(tab_info, expected_thread_id=canonical_thread_id)
        except Exception:
            self.bridge_registry.end_request(bridge_id)
            raise
        return tab_info

    def end_bridge_request(self, bridge_id: str) -> None:
        self.bridge_registry.end_request(bridge_id)

    def release_bridge(self, bridge_id: str) -> bool:
        return self.bridge_registry.release(self.tab_pool, bridge_id)

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

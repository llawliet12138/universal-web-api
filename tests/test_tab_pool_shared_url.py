import threading

from app.core.tab_pool_parts.manager import TabPoolManager


class FakeSession:
    def __init__(self, url):
        self.id = "session-7"
        self.persistent_index = 7
        self._url = url
        self.bridge_owner_id = None

    def get_info(self, use_cached_url=False):
        return {
            "id": self.id,
            "persistent_index": self.persistent_index,
            "url": self._url,
            "url_route_token": "encoded-url",
            "current_domain": "chatgpt.com",
            "route_domain": "chatgpt.com",
        }


def make_manager():
    manager = object.__new__(TabPoolManager)
    manager._condition = threading.Condition(threading.RLock())
    manager._tabs = {}
    manager.max_tabs = 5
    manager._last_scan_time = 0.0
    manager._scan_new_tabs = lambda: None
    manager._start_global_monitor_for_session = lambda session: None
    manager._close_raw_tab = lambda raw_tab_id: True
    return manager


def test_create_shared_url_tab_rejects_non_https_url():
    manager = make_manager()

    result = manager.create_shared_url_tab(
        "http://chatgpt.com/c/123e4567-e89b-12d3-a456-426614174000",
        expected_domain="chatgpt.com",
    )

    assert result == {"ok": False, "error": "invalid_url"}


def test_create_shared_url_tab_rejects_domain_mismatch():
    manager = make_manager()

    result = manager.create_shared_url_tab(
        "https://example.com/c/123e4567-e89b-12d3-a456-426614174000",
        expected_domain="chatgpt.com",
    )

    assert result == {"ok": False, "error": "url_domain_mismatch"}


def test_create_shared_url_tab_registers_valid_shared_cookie_tab():
    manager = make_manager()
    url = "https://chatgpt.com/c/123e4567-e89b-12d3-a456-426614174000"
    fake_tab = object()
    manager._create_shared_tab = lambda *args, **kwargs: {
        "tab": fake_tab,
        "raw_tab_id": "raw-7",
        "browser_context_id": "shared-context",
        "url": url,
    }
    manager._wrap_tab = lambda *args, **kwargs: FakeSession(url)

    result = manager.create_shared_url_tab(url, expected_domain="chatgpt.com")

    assert result["ok"] is True
    assert result["tab"]["persistent_index"] == 7
    assert result["tab"]["tab_route_prefix"] == "/tab/7"
    assert result["tab"]["exact_url_route_prefix"] == "/tab-url/encoded-url"
    assert manager._tabs["session-7"].persistent_index == 7


def test_create_bridge_tab_is_background_owned_and_not_a_new_window():
    manager = make_manager()
    url = "https://chatgpt.com/c/123e4567-e89b-12d3-a456-426614174000"
    captured = {}

    def create_shared_tab(target_url, *, background, new_window):
        captured.update(url=target_url, background=background, new_window=new_window)
        return {
            "tab": object(),
            "raw_tab_id": "raw-7",
            "browser_context_id": "shared-context",
            "url": target_url,
        }

    session = FakeSession(url)
    manager._create_shared_tab = create_shared_tab
    manager._wrap_tab = lambda *args, **kwargs: session

    result = manager.create_shared_url_tab(
        url,
        expected_domain="chatgpt.com",
        background=True,
        new_window=False,
        owner_id="bridge-owner",
    )

    assert result["ok"] is True
    assert captured == {"url": url, "background": True, "new_window": False}
    assert session.bridge_owner_id == "bridge-owner"

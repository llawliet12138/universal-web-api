import json
import time
import threading
import unittest
from pathlib import Path

from app.services.chatgpt_threads import (
    CHATGPT_CONTINUE_PRESET,
    ChatGPTBridgeRegistry,
    ChatGPTThreadService,
    extract_thread_id,
    normalize_thread_candidates,
    normalize_thread_id,
    thread_url,
)


THREAD_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeTab:
    def __init__(self, sidebar_items=None, login_required=False, url="https://chatgpt.com/"):
        self.sidebar_items = sidebar_items or []
        self.login_required = login_required
        self.url = url
        self.scripts = []

    def run_js(self, script):
        self.scripts.append(script)
        if "login-button" in script:
            return self.login_required
        return list(self.sidebar_items)


class FakeSession:
    def __init__(self, tab):
        self.id = "session-1"
        self.tab = tab


class FakeTabPool:
    def __init__(
        self,
        tabs=None,
        sidebar_items=None,
        login_required=False,
        actual_url=None,
    ):
        self.tabs = list(tabs or [])
        self.sidebar_items = sidebar_items or []
        self.login_required = login_required
        self.actual_url = actual_url
        self.created_urls = []
        self.released = []
        self.closed_owners = []
        self.navigated = []

    def get_tabs_with_index(self):
        return list(self.tabs)

    def acquire_by_index(self, persistent_index, task_id, timeout=None):
        tab_url = self.actual_url
        if tab_url is None:
            tab_url = next(
                (
                    item.get("url")
                    for item in self.tabs
                    if item.get("persistent_index") == persistent_index
                ),
                self.created_urls[-1][0] if self.created_urls else "https://chatgpt.com/",
            )
        return FakeSession(FakeTab(self.sidebar_items, self.login_required, tab_url))

    def release(self, session_id, **kwargs):
        self.released.append((session_id, kwargs))

    def create_shared_url_tab(
        self,
        url,
        expected_domain=None,
        *,
        background=False,
        new_window=True,
        owner_id=None,
    ):
        self.created_urls.append(
            (url, expected_domain, background, new_window, owner_id)
        )
        return {
            "ok": True,
            "tab": {
                "persistent_index": 7,
                "url": url,
                "current_domain": "chatgpt.com",
            },
        }

    def get_owned_tab_info(self, owner_id):
        for url, _domain, _background, _new_window, created_owner in reversed(self.created_urls):
            if created_owner == owner_id:
                return {
                    "persistent_index": 7,
                    "url": url,
                    "current_domain": "chatgpt.com",
                }
        return None

    def navigate_owned_tab(self, owner_id, url, timeout=None):
        self.navigated.append((owner_id, url))
        self.actual_url = url
        return {
            "ok": True,
            "tab": {
                "persistent_index": 7,
                "url": url,
                "current_domain": "chatgpt.com",
            },
        }

    def close_owned_tab(self, owner_id):
        self.closed_owners.append(owner_id)
        return {"ok": True, "closed": True}


class FailingTabPool(FakeTabPool):
    def create_shared_url_tab(self, url, expected_domain=None, **kwargs):
        return {"ok": False, "error": "tab_pool_full"}


class FakeBrowser:
    def __init__(self, tab_pool):
        self.tab_pool = tab_pool


class ChatGPTThreadParsingTests(unittest.TestCase):
    def test_extract_thread_id_accepts_chatgpt_conversation_url(self):
        self.assertEqual(
            extract_thread_id(f"https://chatgpt.com/c/{THREAD_ID}"),
            THREAD_ID,
        )

    def test_extract_thread_id_rejects_non_chatgpt_and_malformed_urls(self):
        self.assertIsNone(extract_thread_id(f"https://example.com/c/{THREAD_ID}"))
        self.assertIsNone(extract_thread_id("https://chatgpt.com/c/not-a-uuid"))
        self.assertIsNone(extract_thread_id("https://chatgpt.com:bad/c/not-a-uuid"))

    def test_thread_id_normalization_rejects_noncanonical_values(self):
        self.assertEqual(normalize_thread_id(THREAD_ID), THREAD_ID)
        self.assertEqual(thread_url(THREAD_ID), f"https://chatgpt.com/c/{THREAD_ID}")
        with self.assertRaises(ValueError):
            normalize_thread_id("not-a-uuid")
        self.assertEqual(normalize_thread_id(THREAD_ID.upper()), THREAD_ID)

    def test_normalize_thread_candidates_deduplicates_and_sanitizes(self):
        result = normalize_thread_candidates(
            [
                {"href": f"/c/{THREAD_ID}", "title": " First conversation "},
                {"href": f"https://chatgpt.com/c/{THREAD_ID}", "title": "Duplicate"},
                {"href": "javascript:alert(1)", "title": "Unsafe"},
                {"href": "/c/not-a-uuid", "title": "Invalid"},
            ]
        )

        self.assertEqual(
            result,
            [
                {
                    "id": THREAD_ID,
                    "title": "First conversation",
                    "url": f"https://chatgpt.com/c/{THREAD_ID}",
                    "is_pinned": False,
                }
            ],
        )

    def test_continue_preset_never_starts_a_new_conversation(self):
        config_path = Path(__file__).parents[1] / "config" / "sites.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        preset = config["chatgpt.com"]["presets"][CHATGPT_CONTINUE_PRESET]

        self.assertEqual(preset["stream_config"]["network"]["parser"], "chatgpt")
        self.assertFalse(
            any(
                str(step.get("target") or "") == "new_chat_btn"
                or str(step.get("action") or "").upper() in {"NEW_CHAT", "NEW_CONVERSATION"}
                for step in preset["workflow"]
            )
        )


class ChatGPTThreadServiceTests(unittest.TestCase):
    def test_list_threads_merges_sidebar_and_open_tabs(self):
        second_id = "223e4567-e89b-12d3-a456-426614174001"
        pool = FakeTabPool(
            tabs=[
                {
                    "persistent_index": 2,
                    "url": f"https://chatgpt.com/c/{THREAD_ID}",
                    "current_domain": "chatgpt.com",
                }
            ],
            sidebar_items=[
                {"href": f"/c/{THREAD_ID}", "title": "Sidebar title", "is_pinned": True},
                {"href": f"/c/{second_id}", "title": "Second"},
            ],
        )

        result = ChatGPTThreadService(FakeBrowser(pool)).list_threads()

        self.assertEqual([item["id"] for item in result], [THREAD_ID, second_id])
        self.assertEqual(result[0]["tab_index"], 2)
        self.assertTrue(result[0]["is_open"])
        self.assertTrue(result[0]["is_pinned"])
        self.assertEqual(result[1]["tab_index"], None)
        self.assertFalse(result[1]["is_pinned"])
        self.assertTrue(pool.released)

    def test_ensure_thread_tab_reuses_an_open_conversation(self):
        pool = FakeTabPool(
            tabs=[
                {
                    "persistent_index": 3,
                    "url": f"https://chatgpt.com/c/{THREAD_ID}",
                    "current_domain": "chatgpt.com",
                }
            ]
        )

        result = ChatGPTThreadService(FakeBrowser(pool)).ensure_thread_tab(THREAD_ID)

        self.assertEqual(result["persistent_index"], 3)
        self.assertEqual(pool.created_urls, [])

    def test_ensure_thread_tab_opens_missing_conversation_with_shared_cookies(self):
        pool = FakeTabPool()

        result = ChatGPTThreadService(FakeBrowser(pool)).ensure_thread_tab(THREAD_ID)

        self.assertEqual(result["persistent_index"], 7)
        self.assertEqual(
            pool.created_urls,
            [(f"https://chatgpt.com/c/{THREAD_ID}", "chatgpt.com", True, False, None)],
        )

    def test_thread_tab_creation_errors_are_propagated(self):
        service = ChatGPTThreadService(FakeBrowser(FailingTabPool()))
        with self.assertRaisesRegex(RuntimeError, "tab_pool_full"):
            service.ensure_thread_tab(THREAD_ID)
        with self.assertRaisesRegex(RuntimeError, "tab_pool_full"):
            service.create_new_thread_tab()

    def test_thread_operations_reject_logged_out_chatgpt_session(self):
        pool = FakeTabPool(login_required=True)
        service = ChatGPTThreadService(FakeBrowser(pool))

        with self.assertRaisesRegex(RuntimeError, "chatgpt_login_required"):
            service.create_new_thread_tab()
        with self.assertRaisesRegex(RuntimeError, "chatgpt_login_required"):
            service.ensure_thread_tab(THREAD_ID)

        self.assertEqual(len(pool.released), 2)

    def test_existing_thread_rejects_redirect_to_different_page(self):
        pool = FakeTabPool(actual_url="https://chatgpt.com/")
        service = ChatGPTThreadService(FakeBrowser(pool))

        with self.assertRaisesRegex(RuntimeError, "chatgpt_thread_not_found"):
            service.ensure_thread_tab(THREAD_ID)

    def test_get_thread_for_tab_returns_current_url_or_none(self):
        pool = FakeTabPool(
            tabs=[
                {
                    "persistent_index": 3,
                    "url": f"https://chatgpt.com/c/{THREAD_ID}",
                    "current_domain": "chatgpt.com",
                },
                {"persistent_index": 4, "url": "https://chatgpt.com/"},
            ]
        )
        service = ChatGPTThreadService(FakeBrowser(pool))

        self.assertEqual(service.get_thread_for_tab(3)["id"], THREAD_ID)
        self.assertIsNone(service.get_thread_for_tab(4))
        self.assertIsNone(service.get_thread_for_tab(99))

    def test_service_requires_tab_pool(self):
        with self.assertRaisesRegex(RuntimeError, "tab_pool_unavailable"):
            ChatGPTThreadService(object()).list_threads()

    def test_continue_preset_name_is_stable_for_api_clients(self):
        self.assertEqual(CHATGPT_CONTINUE_PRESET, "继续当前会话")


class ChatGPTBridgeRegistryTests(unittest.TestCase):
    def test_bridge_uses_one_background_tab_and_navigates_it_on_switch(self):
        pool = FakeTabPool()
        registry = ChatGPTBridgeRegistry(ttl_seconds=1800, start_cleaner=False)
        service = ChatGPTThreadService(FakeBrowser(pool), bridge_registry=registry)
        bridge_id = "323e4567-e89b-12d3-a456-426614174002"
        second_id = "423e4567-e89b-12d3-a456-426614174003"

        first = service.activate_bridge(bridge_id, THREAD_ID)
        second = service.activate_bridge(bridge_id, second_id)

        self.assertEqual(first["persistent_index"], second["persistent_index"])
        self.assertEqual(len(pool.created_urls), 1)
        self.assertEqual(
            pool.created_urls[0],
            (f"https://chatgpt.com/c/{THREAD_ID}", "chatgpt.com", True, False, bridge_id),
        )
        self.assertEqual(pool.navigated, [(bridge_id, f"https://chatgpt.com/c/{second_id}")])

    def test_different_bridge_ids_get_different_owned_tabs(self):
        pool = FakeTabPool()
        registry = ChatGPTBridgeRegistry(ttl_seconds=1800, start_cleaner=False)
        service = ChatGPTThreadService(FakeBrowser(pool), bridge_registry=registry)

        service.activate_bridge("523e4567-e89b-12d3-a456-426614174004", THREAD_ID)
        service.activate_bridge("623e4567-e89b-12d3-a456-426614174005", THREAD_ID)

        self.assertEqual(len(pool.created_urls), 2)
        self.assertNotEqual(pool.created_urls[0][-1], pool.created_urls[1][-1])

    def test_release_and_expiry_close_only_owned_bridge_tabs(self):
        pool = FakeTabPool()
        registry = ChatGPTBridgeRegistry(ttl_seconds=1, start_cleaner=False)
        service = ChatGPTThreadService(FakeBrowser(pool), bridge_registry=registry)
        bridge_id = "723e4567-e89b-12d3-a456-426614174006"
        expired_id = "823e4567-e89b-12d3-a456-426614174007"

        service.activate_bridge(bridge_id, THREAD_ID)
        service.release_bridge(bridge_id)
        service.activate_bridge(expired_id, THREAD_ID)
        registry._leases[expired_id].last_activity = time.time() - 2
        registry.cleanup_expired(pool)

        self.assertEqual(pool.closed_owners, [bridge_id, expired_id])

    def test_switch_waits_until_active_request_finishes(self):
        pool = FakeTabPool()
        registry = ChatGPTBridgeRegistry(ttl_seconds=1800, start_cleaner=False)
        service = ChatGPTThreadService(FakeBrowser(pool), bridge_registry=registry)
        bridge_id = "c23e4567-e89b-12d3-a456-426614174011"
        second_id = "d23e4567-e89b-12d3-a456-426614174012"
        service.begin_bridge_request(bridge_id, THREAD_ID)
        switched = threading.Event()

        def switch_thread():
            service.activate_bridge(bridge_id, second_id)
            switched.set()

        worker = threading.Thread(target=switch_thread)
        worker.start()
        time.sleep(0.05)
        self.assertFalse(switched.is_set())
        self.assertEqual(pool.navigated, [])

        service.end_bridge_request(bridge_id)
        worker.join(timeout=1.0)

        self.assertTrue(switched.is_set())
        self.assertEqual(pool.navigated[-1], (bridge_id, f"https://chatgpt.com/c/{second_id}"))


if __name__ == "__main__":
    unittest.main()

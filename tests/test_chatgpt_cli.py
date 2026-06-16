import json
import os
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

import chatgpt_cli
from chatgpt_cli import ChatGPTWebClient


THREAD_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeStreamResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def readline(self):
        return self.read()


def test_client_lists_threads_with_bearer_auth():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"threads": [{"id": THREAD_ID}], "count": 1})

    client = ChatGPTWebClient(
        "http://127.0.0.1:8199",
        token="secret",
        timeout=12,
        opener=opener,
    )

    threads = client.list_threads()

    assert threads == [{"id": THREAD_ID}]
    assert captured["request"].full_url == "http://127.0.0.1:8199/threads"
    assert captured["request"].headers["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 12


def test_client_continues_and_creates_web_threads():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/thread/new"):
            return FakeResponse({"message": "first", "thread_id": THREAD_ID, "tab_index": 2})
        return FakeResponse({"message": "next", "thread_id": THREAD_ID, "tab_index": 2})

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)

    created = client.new_thread("hello")
    continued = client.chat(THREAD_ID, "continue")

    assert created["thread_id"] == THREAD_ID
    assert continued["message"] == "next"
    assert json.loads(requests[0].data) == {"message": "hello", "model": "web-browser"}
    assert requests[1].full_url.endswith(f"/thread/{THREAD_ID}/chat")
    assert all(request.headers["X-chatgpt-bridge-id"] == client.bridge_id for request in requests)


def test_client_activates_and_releases_its_bridge():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse({"ok": True})

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)
    client.activate(THREAD_ID)
    client.release()

    assert requests[0].method == "POST"
    assert requests[0].full_url.endswith(f"/api/chatgpt/bridge/{client.bridge_id}/activate")
    assert json.loads(requests[0].data) == {"thread_id": THREAD_ID}
    assert requests[1].method == "DELETE"


def test_client_streams_existing_thread_chunks():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeStreamResponse([
            b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)

    chunks = list(client.stream_chat(THREAD_ID, "continue"))

    assert chunks == ["hel", "lo"]
    assert requests[0].full_url.endswith(f"/api/chatgpt/threads/{THREAD_ID}/v1/chat/completions")
    assert json.loads(requests[0].data) == {
        "model": "web-browser",
        "stream": True,
        "messages": [{"role": "user", "content": "continue"}],
    }


def test_client_rejects_non_local_base_url():
    with pytest.raises(ValueError, match="localhost"):
        ChatGPTWebClient("https://example.com")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (URLError("offline"), "请先在另一个终端运行 python3 start.py"),
        (
            HTTPError(
                "http://127.0.0.1:8199/threads",
                401,
                "Unauthorized",
                {},
                BytesIO(b'{"detail":"bad token"}'),
            ),
            "HTTP 401",
        ),
    ],
)
def test_client_translates_transport_errors(error, message):
    def opener(request, timeout):
        raise error

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)
    with pytest.raises(RuntimeError, match=message):
        client.list_threads()


def test_client_explains_empty_service_unavailable_response():
    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            BytesIO(b""),
        )

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)
    with pytest.raises(RuntimeError, match="受控浏览器已启动并登录 ChatGPT"):
        client.list_threads()


def test_client_explains_chatgpt_login_required_response():
    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"detail":"chatgpt_login_required"}'),
        )

    client = ChatGPTWebClient("http://127.0.0.1:8199", opener=opener)
    with pytest.raises(RuntimeError, match="受控浏览器中登录 ChatGPT"):
        client.list_threads()


def test_client_rejects_invalid_json_and_non_object_payloads():
    class RawResponse(FakeResponse):
        def read(self):
            return b"not-json"

    client = ChatGPTWebClient(
        "http://127.0.0.1:8199",
        opener=lambda request, timeout: RawResponse({}),
    )
    with pytest.raises(RuntimeError, match="无效 JSON"):
        client.list_threads()

    client = ChatGPTWebClient(
        "http://127.0.0.1:8199",
        opener=lambda request, timeout: FakeResponse(["not", "an", "object"]),
    )
    with pytest.raises(RuntimeError, match="格式无效"):
        client.list_threads()


def test_choose_thread_and_interactive_commands(monkeypatch, capsys):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def list_threads(self):
            return [
                {"id": THREAD_ID, "title": "Existing", "is_open": True},
                {"id": "223e4567-e89b-12d3-a456-426614174001", "title": "Other"},
            ]

        def stream_chat(self, thread_id, message, model):
            self.calls.append(("stream_chat", thread_id, message, model))
            yield "continued"

        def new_thread(self, message, model):
            self.calls.append(("new", message, model))
            return {"message": "created", "thread_id": THREAD_ID}

        def activate(self, thread_id):
            self.calls.append(("activate", thread_id))

        def release(self):
            self.calls.append(("release",))

    client = FakeClient()
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert chatgpt_cli._choose_thread(client) == THREAD_ID

    answers = iter(["hello", "/new", "fresh", "/threads", "2", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert chatgpt_cli.run_interactive(client, THREAD_ID, "web-browser") == 0
    assert client.calls[0] == ("activate", THREAD_ID)
    assert client.calls[1][0] == "stream_chat"
    assert client.calls[2] == ("activate", None)
    assert client.calls[3][0] == "new"
    assert client.calls[-1] == ("release",)
    output = capsys.readouterr().out
    assert "continued" in output
    assert "created" in output
    assert "ChatGPT CLI 会话开始" in output
    assert "已切换会话" in output
    assert THREAD_ID not in output


def test_thread_picker_shows_titles_in_pages_without_ids(monkeypatch, capsys):
    ids = [
        f"123e4567-e89b-12d3-a456-4266141740{i:02d}"
        for i in range(12)
    ]

    class PagedClient:
        def __init__(self):
            self.calls = 0

        def list_threads(self):
            self.calls += 1
            return [
                {
                    "id": thread_id,
                    "title": f"Conversation {index + 1}",
                    "is_pinned": index == 0,
                }
                for index, thread_id in enumerate(ids)
            ]

    client = PagedClient()
    answers = iter(["m", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(
        chatgpt_cli.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size((100, 24)),
    )

    assert chatgpt_cli._choose_thread(client) == ids[11]

    output = capsys.readouterr().out
    assert client.calls == 1
    assert "会话 1-10 / 12" in output
    assert "会话 11-12 / 12" in output
    assert "Conversation 1" in output
    assert "Conversation 11" in output
    assert "只显示标题" not in output
    assert ids[0] not in output
    assert ids[11] not in output


def test_thread_picker_uses_single_key_page_indexes():
    assert chatgpt_cli._thread_index_from_key("0", 10) == 0
    assert chatgpt_cli._thread_index_from_key("1", 10) == 1
    assert chatgpt_cli._thread_index_from_key("9", 10) == 9
    assert chatgpt_cli._thread_index_from_key("9", 2) is None
    assert chatgpt_cli._thread_key_for_local_index(0) == "0"
    assert chatgpt_cli._thread_key_for_local_index(9) == "9"


def test_choose_thread_rejects_bad_number_and_handles_empty_list(monkeypatch, capsys):
    class EmptyClient:
        def list_threads(self):
            return []

    assert chatgpt_cli._choose_thread(EmptyClient()) is None
    assert "没有发现" in capsys.readouterr().out

    class OneClient:
        def list_threads(self):
            return [{"id": THREAD_ID, "title": "Only"}]

    monkeypatch.setattr("builtins.input", lambda prompt: "99")
    with pytest.raises(RuntimeError, match="编号无效"):
        chatgpt_cli._choose_thread(OneClient())


def test_main_reports_invalid_base_url(capsys):
    assert chatgpt_cli.main(["--base-url", "https://example.com", "--new"]) == 1
    assert "错误:" in capsys.readouterr().err

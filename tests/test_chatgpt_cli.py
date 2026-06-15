import json
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


def test_client_rejects_non_local_base_url():
    with pytest.raises(ValueError, match="localhost"):
        ChatGPTWebClient("https://example.com")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (URLError("offline"), "无法连接本地服务"),
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

        def chat(self, thread_id, message, model):
            self.calls.append(("chat", thread_id, message, model))
            return {"message": "continued", "thread_id": thread_id}

        def new_thread(self, message, model):
            self.calls.append(("new", message, model))
            return {"message": "created", "thread_id": THREAD_ID}

    client = FakeClient()
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert chatgpt_cli._choose_thread(client) == THREAD_ID

    answers = iter(["hello", "/new", "fresh", "/threads", "2", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert chatgpt_cli.run_interactive(client, THREAD_ID, "web-browser") == 0
    assert client.calls[0][0] == "chat"
    assert client.calls[1][0] == "new"
    output = capsys.readouterr().out
    assert "continued" in output
    assert "created" in output


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

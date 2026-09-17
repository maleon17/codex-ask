"""Regression coverage for the CodexAsk worker review findings.

Every app-server and MCP dependency in this file is a local fake: these tests
must be runnable from a clean checkout without a Codex login, Telegram, or a
live queue directory.
"""

import importlib.util
import json
import queue
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[1]


def load_worker():
    spec = importlib.util.spec_from_file_location("review_worker", ROOT / "codex_ask_watcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, notification, log, **kwargs):
        self.notification = notification
        self.log = log
        self.kwargs = kwargs
        self.calls = []
        self.closed = threading.Event()

    def start_if_needed(self):
        pass

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "thread/start":
            return {"thread": {"id": "thread-current"}}
        if method == "turn/start":
            self.notification("turn/completed", {
                "threadId": "thread-current", "turn": {"id": "turn-current"},
            })
            return {"turn": {"id": "turn-current"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)

    def close(self, timeout=5):
        self.closed.set()


def isolated_worker(monkeypatch, tmp_path):
    worker_module = load_worker()
    monkeypatch.setattr(worker_module, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(worker_module, "RESULT_DIR", tmp_path / "result")
    monkeypatch.setattr(worker_module, "RESET_DIR", tmp_path / "reset")
    monkeypatch.setattr(worker_module, "TOOL_CONTEXT_DIR", tmp_path / "tool-context")
    monkeypatch.setattr(worker_module, "SESSIONS_FILE", tmp_path / "sessions.json")
    worker_module.ensure_dirs()
    return worker_module


def test_late_turn_completed_is_ignored_for_the_next_turn(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    state = w.TurnState("second")
    state.thread_id = "thread-second"
    state.turn_id = "turn-second"
    session.active = state

    # This is a late completion from a prior stateless search/translate turn.
    session._notification("turn/completed", {
        "threadId": "thread-first", "turn": {"id": "turn-first"},
    })

    assert not state.done.is_set()


def test_unidentified_next_turn_buffers_then_discards_a_late_completion(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    state = w.TurnState("next")
    state.thread_id = "thread-shared"
    session.active = state

    # The next turn has not received its turn/start result yet. A late event
    # from the preceding stateless turn must not claim this state merely
    # because both used the same app-server session.
    session._notification("turn/completed", {
        "threadId": "thread-shared", "turn": {"id": "old-search-turn"},
    })
    state.turn_id = "new-chat-turn"
    for method, params in state.pending_notifications:
        session._notification(method, params)

    assert not state.done.is_set()


def test_timeout_interrupts_and_confirms_before_publishing_terminal_result(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    monkeypatch.setattr(w, "TURN_TIMEOUT", 0)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    # Keep the fake turn open; the timeout path must issue turn/interrupt.
    session.client.notification = lambda *_: None

    session.handle({"request_id": "timed-out", "question": "hello"})

    assert [call[0] for call in session.client.calls] == [
        "thread/start", "turn/start", "turn/interrupt",
    ]
    result = json.loads((w.RESULT_DIR / "timed-out.json").read_text())
    assert result["done"] is True


def test_tool_context_is_per_turn_and_forwards_topic_and_placeholder(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    writes = []
    original_atomic_json = w._atomic_json

    def record(path, value):
        if path == w._tool_context_path("instance", "42"):
            writes.append(dict(value))
        original_atomic_json(path, value)

    monkeypatch.setattr(w, "_atomic_json", record)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    session.handle({
        "request_id": "first", "question": "one", "requester_id": "owner",
        "topic_id": 11, "message_id": 101,
    })
    session.handle({
        "request_id": "second", "question": "two", "requester_id": "owner",
        "topic_id": 22, "message_id": 202,
    })

    assert writes == [
        {"request_id": "first", "requester_id": "owner", "topic_id": 11, "message_id": 101,
         "chat_id": "42"},
        {"request_id": "second", "requester_id": "owner", "topic_id": 22, "message_id": 202,
         "chat_id": "42"},
    ]


def test_trigger_turn_resumes_the_interactive_chat_thread(monkeypatch, tmp_path):
    """A trigger must continue the chat's thread, not start an isolated one."""
    w = isolated_worker(monkeypatch, tmp_path)

    class ResumeClient(FakeClient):
        def request(self, method, params=None, timeout=None):
            self.calls.append((method, params, timeout))
            if method == "thread/resume":
                assert params["threadId"] == "interactive-thread"
                return {"thread": {"id": "interactive-thread"}}
            if method == "turn/start":
                self.notification("turn/completed", {
                    "threadId": "interactive-thread", "turn": {"id": "trigger-turn"},
                })
                return {"turn": {"id": "trigger-turn"}}
            raise AssertionError(method)

    monkeypatch.setattr(w, "AppServerClient", ResumeClient)
    index = w.SessionIndex(w.SESSIONS_FILE)
    index.set("instance", "42", "interactive-thread")
    session = w.ChatSession("instance", "42", index)
    session.handle({
        "request_id": "trigger", "mode": "chat", "resume_session": True,
        "question": "automatic reply", "requester_id": "trigger:rule",
        "chat_context": "[id=1, Анна]: Джарвис, ты тут?",
    })

    methods = [method for method, _, _ in session.client.calls]
    assert methods == ["thread/resume", "turn/start"]
    prompt = session.client.calls[1][1]["input"][0]["text"]
    assert "Контекст текущего чата" in prompt
    assert "Джарвис, ты тут?" in prompt


def load_mcp(monkeypatch, tmp_path):
    class Server:
        def __init__(self, name):
            self.name = name
        def tool(self):
            return lambda function: function

    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    mcpserver = types.ModuleType("mcp.server.mcpserver")
    mcpserver.MCPServer = Server
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", mcpserver)
    monkeypatch.setenv("CODEX_TELEGRAM_INSTANCE_ID", "instance")
    monkeypatch.setenv("CODEX_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("CODEX_TELEGRAM_CONTEXT_DIR", str(tmp_path))
    for key in ("CODEX_TELEGRAM_TOPIC_ID", "CODEX_TELEGRAM_EXCLUDE_MSG_ID", "TOPIC_ID", "EXCLUDE_MSG_ID"):
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location("review_mcp", ROOT / "telegram_actions_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_reads_topic_and_excluded_message_from_dynamic_turn_context(monkeypatch, tmp_path):
    module = load_mcp(monkeypatch, tmp_path)
    path = tmp_path / (module.hashlib.sha256(b"instance\0" + b"42").hexdigest() + ".json")
    seen = []
    monkeypatch.setattr(module, "_call_tool", lambda tool, args: seen.append((tool, args)) or "ok")

    path.write_text(json.dumps({"requester_id": "owner", "topic_id": 11, "message_id": 101}))
    module.read_history()
    path.write_text(json.dumps({"requester_id": "owner", "topic_id": 22, "message_id": 202}))
    module.read_history()

    assert seen == [
        ("read_history", {"count": 50, "direction": None, "reply_id": None, "until_id": None,
                          "topic_id": 11, "exclude_id": 101, "chat": ""}),
        ("read_history", {"count": 50, "direction": None, "reply_id": None, "until_id": None,
                          "topic_id": 22, "exclude_id": 202, "chat": ""}),
    ]


def test_classify_uses_a_fresh_restricted_client(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    clients = []

    class ClassifyClient(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(w, "AppServerClient", ClassifyClient)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    ordinary_client = session.client
    session.handle({"request_id": "classify", "mode": "classify", "question": "yes?"})

    assert len(clients) == 2
    assert clients[1] is not ordinary_client
    assert clients[1].kwargs["extra_args"] == ["-c", "mcp_servers={}"]
    turn_params = next(params for method, params, _ in clients[1].calls if method == "turn/start")
    assert turn_params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert clients[1].closed.is_set()


def test_transient_resume_error_keeps_old_thread_but_missing_thread_is_reported(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)

    class ResumeClient(FakeClient):
        def __init__(self, *args, error, **kwargs):
            super().__init__(*args, **kwargs)
            self.error = error
        def request(self, method, params=None, timeout=None):
            if method == "thread/resume":
                raise w.AppServerError(self.error)
            return super().request(method, params, timeout)

    index = w.SessionIndex(w.SESSIONS_FILE)
    index.set("instance", "42", "old-thread")
    monkeypatch.setattr(w, "AppServerClient", lambda *args, **kwargs: ResumeClient(*args, error="request timed out", **kwargs))
    session = w.ChatSession("instance", "42", index)
    with pytest.raises(w.AppServerError):
        session._ensure_thread(True)
    assert index.get("instance", "42") == "old-thread"

    monkeypatch.setattr(w, "AppServerClient", lambda *args, **kwargs: ResumeClient(*args, error="thread not found", **kwargs))
    session = w.ChatSession("instance", "42", index)
    assert session._ensure_thread(True) == "thread-current"
    assert session.new_context_notice


def test_ambiguous_empty_resume_does_not_start_a_new_context(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)

    class EmptyResumeClient(FakeClient):
        def request(self, method, params=None, timeout=None):
            if method == "thread/resume":
                return {}
            raise AssertionError(f"must not start a replacement thread: {method}")

    index = w.SessionIndex(w.SESSIONS_FILE)
    index.set("instance", "42", "old-thread")
    monkeypatch.setattr(w, "AppServerClient", EmptyResumeClient)
    session = w.ChatSession("instance", "42", index)

    with pytest.raises(w.AppServerError):
        session._ensure_thread(True)
    assert index.get("instance", "42") == "old-thread"


def test_reset_prevents_an_old_worker_from_restoring_the_session_index(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def request(self, method, params=None, timeout=None):
            if method == "thread/start":
                started.set()
                assert release.wait(1)
                return {"thread": {"id": "late-thread"}}
            raise w.AppServerError("closed")
        def close(self, timeout=5):
            super().close(timeout)

    monkeypatch.setattr(w, "AppServerClient", BlockingClient)
    worker = w.Worker()
    session = worker._session("instance", "42")
    request_thread = threading.Thread(target=session.handle, args=({"request_id": "old", "question": "old"},))
    request_thread.start()
    assert started.wait(1)
    reset = w.RESET_DIR / "reset.json"
    reset.write_text(json.dumps({"instance_id": "instance", "chat_id": "42"}))
    reset_thread = threading.Thread(target=worker._process_reset, args=(reset,))
    reset_thread.start()
    assert session.client.closed.wait(1)
    release.set()
    request_thread.join(1)
    reset_thread.join(1)
    assert not request_thread.is_alive()
    assert not reset_thread.is_alive()
    assert worker.index.get("instance", "42") is None
    worker.close()


def test_close_unblocks_the_waiting_turn(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    state = w.TurnState("waiting")
    session.active = state

    session.close()

    assert state.done.is_set()


class Stream:
    def __init__(self):
        self.items = queue.Queue()
    def __iter__(self):
        while True:
            item = self.items.get()
            if item is None:
                return
            yield item


class Input:
    def __init__(self, output):
        self.output = output
    def write(self, payload):
        message = json.loads(payload)
        if message.get("method") == "initialize":
            self.output.items.put(json.dumps({"id": message["id"], "result": {}}) + "\n")
    def flush(self):
        pass


class Process:
    def __init__(self):
        self.stdout, self.stderr = Stream(), Stream()
        self.stdin, self.code = Input(self.stdout), None
    def poll(self):
        return self.code
    def terminate(self):
        self.code = -15
        self.stdout.items.put(None)
        self.stderr.items.put(None)
    kill = terminate
    def wait(self, timeout=None):
        return self.code


def test_old_app_server_reader_cannot_fail_a_new_generation_request():
    from app_server import AppServerClient

    created = []
    with patch("app_server.subprocess.Popen", side_effect=lambda *args, **kwargs: created.append(Process()) or created[-1]):
        client = AppServerClient(lambda *args: None, lambda *args: None, request_timeout=1)
        client.start()
        old = created[0]
        client.close(timeout=0.01)
        client.start()
        new = created[1]
        outcome = {}
        request_thread = threading.Thread(target=lambda: outcome.setdefault("value", client.request("fresh/request")))
        request_thread.start()
        old.stdout.items.put(None)
        new.stdout.items.put('{"id":3,"result":{"fresh":true}}\n')
        request_thread.join(1)

    assert not request_thread.is_alive()
    assert outcome["value"] == {"fresh": True}

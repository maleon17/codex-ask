"""`.xstop`: a cancel marker interrupts the turn and reports it as cancelled."""
import json
import threading
import time

from test_review_x_regressions import FakeClient, isolated_worker


def _result(w, req_id):
    return json.loads((w.RESULT_DIR / f"{req_id}.json").read_text())


def _open_turn_session(w, monkeypatch):
    """A session whose fake turn never completes on its own."""
    monkeypatch.setattr(w, "AppServerClient", FakeClient)
    session = w.ChatSession("instance", "42", w.SessionIndex(w.SESSIONS_FILE))
    session.client.notification = lambda *_: None
    return session


def test_request_stopped_before_its_turn_never_starts(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    session = _open_turn_session(w, monkeypatch)
    (w.QUEUE_DIR / "early.cancel").write_text("{}")

    session.handle({"request_id": "early", "question": "hello"})

    assert session.client.calls == []
    assert _result(w, "early") == {
        "done": True, "request_id": "early", "cancelled": True, "answer": "", "thoughts": [],
    }
    assert not (w.QUEUE_DIR / "early.cancel").exists()


def test_marker_interrupts_the_active_turn(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    worker = w.Worker()
    try:
        session = _open_turn_session(w, monkeypatch)
        with worker.sessions_lock:
            worker.sessions["instance:42"] = session
        thread = threading.Thread(
            target=session.handle, args=({"request_id": "running", "question": "hello"},),
        )
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not (session.active and session.active.turn_id):
            time.sleep(0.02)
        assert session.active and session.active.turn_id, "the turn never started"

        # A marker for some other request leaves this turn alone.
        (w.QUEUE_DIR / "someone-else.cancel").write_text("{}")
        worker._apply_cancel_markers()
        assert not session.active.cancelled

        (w.QUEUE_DIR / "running.cancel").write_text("{}")
        worker._apply_cancel_markers()
        thread.join(5)
        assert not thread.is_alive(), "the turn was not released"

        assert [call[0] for call in session.client.calls] == [
            "thread/start", "turn/start", "turn/interrupt",
        ]
        assert _result(w, "running")["cancelled"] is True
        assert "error" not in _result(w, "running")["answer"].lower()
        assert not (w.QUEUE_DIR / "running.cancel").exists()
        assert session.active is None
    finally:
        worker.close()


def test_abandoned_marker_is_eventually_dropped(monkeypatch, tmp_path):
    w = isolated_worker(monkeypatch, tmp_path)
    worker = w.Worker()
    try:
        marker = w.QUEUE_DIR / "ghost.cancel"
        marker.write_text("{}")
        worker._apply_cancel_markers(now=time.time())
        assert marker.exists()
        worker._apply_cancel_markers(now=time.time() + w.CANCEL_MARKER_TTL_S + 1)
        assert not marker.exists()
    finally:
        worker.close()

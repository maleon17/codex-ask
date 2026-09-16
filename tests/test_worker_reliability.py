import importlib.util
import json
from pathlib import Path


def load_worker():
    path = Path(__file__).parents[1] / "codex_ask_watcher.py"
    spec = importlib.util.spec_from_file_location("worker_reliability", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_processing_file_from_restart_gets_explicit_interrupted_result(monkeypatch, tmp_path):
    w = load_worker()
    monkeypatch.setattr(w, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(w, "RESULT_DIR", tmp_path / "result")
    monkeypatch.setattr(w, "RESET_DIR", tmp_path / "reset")
    monkeypatch.setattr(w, "SESSIONS_FILE", tmp_path / "sessions.json")
    w.ensure_dirs()
    (w.QUEUE_DIR / "r1.json.processing").write_text(json.dumps({"request_id": "r1"}))
    worker = w.Worker()
    worker.recover_processing()
    result = json.loads((w.RESULT_DIR / "r1.json").read_text())
    assert result["done"] is True
    assert "прерван" in result["answer"].lower()


def test_worker_admission_is_bounded(monkeypatch, tmp_path):
    w = load_worker()
    monkeypatch.setattr(w, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(w, "RESULT_DIR", tmp_path / "result")
    monkeypatch.setattr(w, "RESET_DIR", tmp_path / "reset")
    monkeypatch.setattr(w, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(w, "WORKER_QUEUE_MAX", 1)
    w.ensure_dirs()
    worker = w.Worker()
    assert worker.admit(tmp_path / "one.json") is True
    assert worker.admit(tmp_path / "two.json") is False

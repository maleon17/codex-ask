import asyncio
from unittest.mock import patch

from test_trigger_authorization import codex_ask, make_module


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"status":"ok","path":"/artifact"}'


def test_codex_client_enqueues_with_bearer_token(monkeypatch):
    """S01: CodexAsk also authenticates its /xask relay request."""
    bot = make_module()
    monkeypatch.setattr(codex_ask, "RELAY_TOKEN", "codex-secret")
    captured = []
    monkeypatch.setattr(bot, "_relay_open", lambda request, *_: captured.append(request) or _Response())

    assert bot._enqueue("question", "7", "request-7")[0]
    assert captured[0].get_header("Authorization") == "Bearer codex-secret"


def test_codex_upload_boundary_is_absent_from_file_bytes(monkeypatch):
    """S03: a collision in the first random boundary is retried."""
    bot = make_module()
    captured = []
    monkeypatch.setattr(codex_ask, "RELAY_TOKEN", "codex-secret")
    monkeypatch.setattr(codex_ask, "RELAY_OPENER", type("O", (), {"open": lambda _, request, **__: captured.append(request) or _Response()})())

    with patch.object(codex_ask.secrets, "token_hex", side_effect=["collision", "safe-boundary"]):
        assert asyncio.run(bot._upload_to_lightrag(b"collision in file", "proof.bin")) == "/artifact"
    boundary = captured[0].get_header("Content-type").rsplit("=", 1)[1].encode()
    file_bytes = captured[0].data.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    assert boundary not in file_bytes

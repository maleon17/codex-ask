"""CodexAsk mirror regressions; all transport and Telegram objects are faked."""
import asyncio
from unittest.mock import AsyncMock

from test_trigger_authorization import codex_ask, make_module


def run(awaitable):
    return asyncio.run(awaitable)


class Message:
    def __init__(self):
        self.text = self.raw_text = "🤔 Думаю"
        self.id = 9
        self.edits = []
        self.respond = AsyncMock()

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))


def test_telegram_text_limit_resolves_without_test_side_overrides():
    """Regression: TELEGRAM_TEXT_LIMIT was a bare module-level name, not a
    class attribute, so self.TELEGRAM_TEXT_LIMIT raised AttributeError on
    every real instance -- _dispatch_answer crashed before delivering ANY
    final answer, short or long."""
    bot = make_module()
    message = Message()
    run(bot._dispatch_answer(None, 1, "q", "chat", 0, message, "short answer", []))
    assert message.edits and "short answer" in message.edits[-1][0]


def test_safe_edit_plain_fallback_explicitly_disables_html():
    class HtmlDefault(Message):
        async def edit(self, text, **kwargs):
            self.edits.append((text, kwargs))
            if len(self.edits) == 1:
                raise ValueError("bad html")
    message = HtmlDefault()
    run(make_module()._safe_edit(message, "<literal>", parse_mode="html"))
    assert message.edits[-1] == ("<literal>", {"parse_mode": None})


def test_enqueue_parses_application_error(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"status":"error","message":"offline"}'
    bot = make_module()
    monkeypatch.setattr(bot, "_relay_open", lambda *args: Response())
    assert bot._enqueue("q", 1, "r") == (False, "offline")


def test_enqueue_accepts_the_relay_own_success_statuses(monkeypatch):
    """Regression: cmd_queue.py's /ask and /xask never return status "ok" --
    a fresh request is "queued", an idempotent retry is "accepted". Checking
    for == "ok" instead of == "error" treated every real success as a
    rejected request."""
    import json
    bot = make_module()
    for status in ("queued", "accepted"):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, status=status): return json.dumps({"status": status}).encode()
        monkeypatch.setattr(bot, "_relay_open", lambda *args, status=status: Response())
        assert bot._enqueue("q", 1, "r") == (True, "")


def test_history_anchor_waits_for_enqueue_ack():
    bot = make_module()
    bot.db.values = {}
    bot.db.get = lambda ns, key, default=None: bot.db.values.get(key, default)
    bot.db.set = lambda ns, key, value: bot.db.values.__setitem__(key, value)
    bot._get_chat_history = AsyncMock(return_value="history")
    message = type("M", (), {"chat_id": 1, "id": 4, "reply_to": None})()
    _, _, pending = run(bot._get_chat_history_delta(message))
    assert bot.db.values == {}
    bot._commit_history_anchor(pending)
    assert bot.db.values["last_seen_id_1"] == 4


def test_persona_read_failure_does_not_move_index():
    bot = make_module()
    bot._persona_sessions = {"s": {"sid": "s", "pages": ["a", "b"], "index": 0, "chat_id": 1, "code_msg_id": 2}}
    bot._client = type("C", (), {"get_messages": AsyncMock(side_effect=OSError())})()
    bot._persona_ack = AsyncMock()
    run(bot._persona_nav(object(), "s", 1))
    assert bot._persona_sessions["s"]["index"] == 0


class RealShapeDB(dict):
    """Mirrors the live Heroku framework's Database class exactly: it IS a
    dict, {owner: {key: value}}, with .get()/.set() overriding the 2-arg
    dict.get() with a 3-arg (owner, key, default) signature. Regression for
    a real bug: _clear_history_anchors() used to probe for a ._db or
    .values attribute holding a flat mapping, neither of which exists on
    the real Database(dict) -- topic-scoped cursors were silently never
    cleared. See /Heroku/heroku/database.py on the live userbot host."""

    def get(self, owner, key=None, default=None):
        try:
            return self[owner][key]
        except KeyError:
            return default

    def set(self, owner, key, value):
        self.setdefault(owner, {})[key] = value
        return True


def test_reset_clears_every_topic_cursor_for_the_chat():
    bot = make_module()
    bot.db = RealShapeDB()
    bot.db.set("CodexAsk", "last_seen_id_1", 10)
    bot.db.set("CodexAsk", "last_seen_id_1_55", 20)
    bot.db.set("CodexAsk", "last_seen_id_1_77", 30)
    bot.db.set("CodexAsk", "last_seen_id_12", 40)
    bot.db.set("CodexAsk", "unrelated", "x")

    bot._clear_history_anchors(1)

    ns = dict.get(bot.db, "CodexAsk")
    assert ns["last_seen_id_1"] is None
    assert ns["last_seen_id_1_55"] is None
    assert ns["last_seen_id_1_77"] is None
    assert ns["last_seen_id_12"] == 40
    assert ns["unrelated"] == "x"

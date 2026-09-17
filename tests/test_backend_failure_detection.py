import importlib.util
import asyncio
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_jarvis_ask():
    """Load the userbot module with the two external imports stubbed."""
    package_name = "_jarvis_ask_test_package"

    package = types.ModuleType(package_name)
    package.__path__ = []
    subpackage_name = f"{package_name}.modules"
    subpackage = types.ModuleType(subpackage_name)
    subpackage.__path__ = [str(ROOT)]

    loader = types.ModuleType(f"{package_name}.loader")

    class Module:
        pass

    loader.Module = Module
    loader.tds = lambda value: value

    herokutl = types.ModuleType("herokutl")
    herokutl.__path__ = []
    herokutl_tl = types.ModuleType("herokutl.tl")
    herokutl_tl.__path__ = []
    custom = types.ModuleType("herokutl.tl.custom")
    custom.Message = type("Message", (), {})

    sys.modules[package_name] = package
    sys.modules[subpackage_name] = subpackage
    sys.modules[f"{package_name}.loader"] = loader
    sys.modules["herokutl"] = herokutl
    sys.modules["herokutl.tl"] = herokutl_tl
    sys.modules["herokutl.tl.custom"] = custom

    module_name = f"{subpackage_name}.jarvis_ask"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "jarvis_ask.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


jarvis_ask = _load_jarvis_ask()


def test_rate_limit_explanation_is_not_a_backend_failure():
    answer = "Rate limit — это ограничение частоты запросов API, а quota задаёт общий бюджет."

    assert not jarvis_ask.JarvisAsk.is_failure(answer)


def test_worker_error_prefix_still_triggers_fallback_detection():
    answer = "\n  Ошибка воркера: Connection refused"

    assert jarvis_ask.JarvisAsk.is_failure(answer)


def test_codex_worker_error_prefixes_are_still_detected():
    answers = (
        "⚠️ Ошибка Codex: Connection refused",
        "⚠️ Ошибка очереди: malformed request",
        "⚠️ Codex не завершил запрос за отведённое время.",
        "⚠️ Лимит аккаунта Codex исчерпан. Проверь /usage.",
    )

    assert all(jarvis_ask.JarvisAsk.is_failure(answer) for answer in answers)


def test_error_keyword_later_in_model_prose_is_not_a_failure():
    answer = "Я объясню, что такое quota; это не ошибка воркера и повторять запрос не нужно."

    assert not jarvis_ask.JarvisAsk.is_failure(answer)


def test_coordinator_deduplicates_watchers_and_applies_cross_engine_priority():
    class Backend:
        def __init__(self):
            self.fired = []
        async def _is_trigger_exempt(self, trigger, message): return False
        async def _trigger_matches(self, trigger, message): return True
        async def _fire_triggers(self, triggers, message): self.fired.extend(triggers)

    claude, codex = Backend(), Backend()
    coordinator = jarvis_ask.JarvisAsk()
    coordinator.db = types.SimpleNamespace(get=lambda *_: {"1": [
        {"engine": "claude", "action": "delete"},
        {"engine": "codex", "action": "reply"},
    ]})
    coordinator.lookup = lambda name: claude if name == "ClaudeAsk" else codex
    message = jarvis_ask.Message()
    message.chat_id, message.id, message.out = 1, 7, False
    asyncio.run(coordinator.handle_message(message, "claude", claude))
    asyncio.run(coordinator.handle_message(message, "codex", codex))
    assert [t["action"] for t in claude.fired] == ["delete"]
    assert codex.fired == []

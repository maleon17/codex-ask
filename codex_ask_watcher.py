#!/usr/bin/env python3
"""Persistent Codex backend for the separate ``CodexAsk`` userbot module.

The Telethon/Heroku module lives in :mod:`codex_ask`; this process is only a
headless model worker.  It consumes ``/xask`` queue files written by the
shared ``cmd_queue.py`` transport and publishes the progress/result files the
module polls.  It never owns a Telegram bot token and never polls Telegram.

One app-server child is kept per ``(instance_id, chat_id)`` for the lifetime
of this process.  That gives ``.xask`` the same persistent-thread behaviour as
``.ask`` without making the standalone Bot API application a dependency.
Telegram actions are exposed by the sibling MCP server; the CodexAsk module's
``tool_call_watcher`` executes those calls through the real userbot session.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import queue
import threading
import re
import time
import uuid
from pathlib import Path

from app_server import AppServerClient, AppServerError


ROOT = Path(__file__).resolve().parent


def _shared_state_dir(env_name, leaf):
    """Mirrors the relay's cmd_queue.py: private runtime state shared with it
    (never /tmp -- queue items carry chat text and uploaded media). The base
    is 0700; ensure_dirs() creates the leaves. An explicit override still wins."""
    override = os.environ.get(env_name)
    if override:
        return Path(override)
    base = os.environ.get("JARVIS_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "telegram-ai",
    )
    base_path = Path(os.path.abspath(os.path.expanduser(base)))
    base_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_path.chmod(0o700)
    return base_path / leaf


QUEUE_DIR = _shared_state_dir("CODEX_JARVIS_XASK_QUEUE", "xask_queue")
RESULT_DIR = _shared_state_dir("CODEX_JARVIS_XASK_RESULT", "xask_result")
RESET_DIR = _shared_state_dir("CODEX_JARVIS_XRESET_QUEUE", "xask_reset")
TOOL_CONTEXT_DIR = _shared_state_dir("CODEX_JARVIS_TOOL_CONTEXT_DIR", "tool_context")
STATE_DIR = Path(os.environ.get("CODEX_JARVIS_STATE_DIR", str(ROOT / "state")))
SESSIONS_FILE = STATE_DIR / "sessions.json"
CODEX_HOME = Path(os.environ.get("CODEX_JARVIS_CODEX_HOME", str(ROOT / "codex_home")))
INTERNAL_TOOL_RESULT_PREFIX = "[INTERNAL_TOOL_RESULT]"


def _is_internal_tool_result(value):
    return isinstance(value, str) and value.startswith(INTERNAL_TOOL_RESULT_PREFIX)


# Deliberately NOT the home directory by default: Codex reads the AGENTS.md of
# the exact cwd it is given (not of ancestor directories), so a home-directory
# AGENTS.md written for real engineering work would be obeyed on every persona
# turn and every /xclassify call. Read access to the rest of the filesystem is
# unaffected (only writable_roots is cwd-scoped), so nothing is lost.
CODEX_CWD = os.environ.get("CODEX_JARVIS_CWD", os.path.dirname(os.path.abspath(__file__)))
CODEX_SANDBOX = os.environ.get("CODEX_JARVIS_SANDBOX", "danger-full-access")
CODEX_MODEL = os.environ.get("CODEX_JARVIS_MODEL", "gpt-6-luna")
CODEX_EFFORT = os.environ.get("CODEX_JARVIS_EFFORT", "low")
# Tier 1 trigger classify (mirrors claude_watcher.py's CLASSIFY_MODEL for the
# "verify" trigger gate -- the cheap/fast tier for CodexAsk's own triggers).
# Stateless, no persona, no MCP tools needed -- see CLASSIFY_PROMPT below.
CODEX_CLASSIFY_MODEL = os.environ.get("CODEX_JARVIS_CLASSIFY_MODEL", "gpt-6-luna")

# Same 3-way (да/нет/не уверен) contract as claude_watcher.py's "classify"
# mode -- codex_ask.py's _classify_condition parses the answer identically
# regardless of which backend produced it, so the wording must match, not
# just the intent.
CLASSIFY_PROMPT = (
    "Ты классификатор. Тебе дают условие и текст сообщения. Ответь "
    "СТРОГО одним из трёх вариантов: 'да' (уверенно подходит под "
    "условие), 'нет' (уверенно не подходит) или 'не уверен' "
    "(сомнительный, пограничный случай) -- без пояснений, без "
    "форматирования, без знаков препинания. Используй 'не уверен' "
    "честно, когда действительно есть сомнение, а не только 'да'/'нет' "
    "для перестраховки."
)
DEFAULT_INSTANCE = os.environ.get("CODEX_JARVIS_INSTANCE_ID", "default")

# Interface language of a request: the userbot module sends its `language`
# setting with every request. It picks the shipped persona template and the
# language of everything this worker writes itself. A module too old to send
# one was Russian-only, so that stays the default.
LANGUAGES = ("en", "ru", "uk", "kk", "de")
DEFAULT_LANGUAGE = "ru"
_TEXTS = {
    "file_created": {"en": "created", "ru": "создан", "uk": "створено", "kk": "жасалды", "de": "erstellt"},
    "file_deleted": {"en": "deleted", "ru": "удалён", "uk": "видалено", "kk": "жойылды", "de": "gelöscht"},
    "file_changed": {"en": "changed", "ru": "изменён", "uk": "змінено", "kk": "өзгертілді", "de": "geändert"},
    "file_change": {"en": "✏️ File change", "ru": "✏️ Изменение файла", "uk": "✏️ Зміна файлу",
                    "kk": "✏️ Файлды өзгерту", "de": "✏️ Dateiänderung"},
    "tool": {"en": "Tool", "ru": "Инструмент", "uk": "Інструмент", "kk": "Құрал", "de": "Werkzeug"},
    "agent": {"en": "agent", "ru": "агент", "uk": "агент", "kk": "агент", "de": "Agent"},
    "agent_label": {"en": "🤖 Agent · {name}", "ru": "🤖 Агент · {name}", "uk": "🤖 Агент · {name}",
                    "kk": "🤖 Агент · {name}", "de": "🤖 Agent · {name}"},
    "image_view": {"en": "🖼 Viewing an image", "ru": "🖼 Просмотр изображения", "uk": "🖼 Перегляд зображення",
                   "kk": "🖼 Суретті қарау", "de": "🖼 Bild ansehen"},
    "image_generation": {"en": "🎨 Generating an image", "ru": "🎨 Генерация изображения",
                         "uk": "🎨 Генерація зображення", "kk": "🎨 Сурет жасау", "de": "🎨 Bild erzeugen"},
    "compaction": {"en": "🗜 Context compaction", "ru": "🗜 Сжатие контекста", "uk": "🗜 Стиснення контексту",
                   "kk": "🗜 Контексті сығу", "de": "🗜 Kontext komprimieren"},
    "compacted": {"en": "the session context was compacted", "ru": "контекст сессии сжат",
                  "uk": "контекст сесії стиснуто", "kk": "сессия контексі сығылды",
                  "de": "der Sitzungskontext wurde komprimiert"},
    "plan": {"en": "📋 Plan", "ru": "📋 План", "uk": "📋 План", "kk": "📋 Жоспар", "de": "📋 Plan"},
    "seconds": {"en": "{value} s", "ru": "{value} с", "uk": "{value} с", "kk": "{value} с", "de": "{value} s"},
    "waiting": {"en": "⏳ Waiting", "ru": "⏳ Ожидание", "uk": "⏳ Очікування", "kk": "⏳ Күту", "de": "⏳ Warten"},
    "review": {"en": "🔍 Review", "ru": "🔍 Проверка", "uk": "🔍 Перевірка", "kk": "🔍 Тексеру", "de": "🔍 Prüfung"},
    "review_on": {"en": "review mode on", "ru": "режим проверки включён", "uk": "режим перевірки увімкнено",
                  "kk": "тексеру режимі қосылды", "de": "Prüfmodus an"},
    "review_off": {"en": "review mode finished", "ru": "режим проверки завершён", "uk": "режим перевірки завершено",
                   "kk": "тексеру режимі аяқталды", "de": "Prüfmodus beendet"},
    "error": {"en": "❌ Error", "ru": "❌ Ошибка", "uk": "❌ Помилка", "kk": "❌ Қате", "de": "❌ Fehler"},
    "result": {"en": "Result", "ru": "Результат", "uk": "Результат", "kk": "Нәтиже", "de": "Ergebnis"},
    "result_received": {"en": "result received", "ru": "результат получен", "uk": "результат отримано",
                        "kk": "нәтиже алынды", "de": "Ergebnis erhalten"},
    "codex_error": {"en": "Codex error", "ru": "ошибка Codex", "uk": "помилка Codex", "kk": "Codex қатесі",
                    "de": "Codex-Fehler"},
    "usage_limit": {"en": "The Codex account limit is used up. Check /usage and wait for the reset time.",
                    "ru": "Лимит аккаунта Codex исчерпан. Проверь /usage и дождись времени сброса.",
                    "uk": "Ліміт акаунта Codex вичерпано. Перевір /usage і дочекайся часу скидання.",
                    "kk": "Codex аккаунтының лимиті таусылды. /usage тексеріп, қалпына келу уақытын күт.",
                    "de": "Das Limit des Codex-Kontos ist aufgebraucht. Prüfe /usage und warte auf den Reset."},
    "session_budget": {"en": "This session's budget is used up. Use .xnew for a new thread.",
                       "ru": "Бюджет этой сессии исчерпан. Используй .xnew для нового треда.",
                       "uk": "Бюджет цієї сесії вичерпано. Використай .xnew для нового треду.",
                       "kk": "Бұл сессияның бюджеті таусылды. Жаңа тред үшін .xnew қолдан.",
                       "de": "Das Budget dieser Sitzung ist aufgebraucht. Nutze .xnew für einen neuen Thread."},
    "context_full": {"en": "The session context is full. Use .xnew and start a new thread.",
                     "ru": "Контекст сессии исчерпан. Используй .xnew и начни новый тред.",
                     "uk": "Контекст сесії вичерпано. Використай .xnew і почни новий тред.",
                     "kk": "Сессия контексі толды. .xnew қолданып, жаңа тред баста.",
                     "de": "Der Sitzungskontext ist voll. Nutze .xnew und starte einen neuen Thread."},
    "context_lost": {"en": "The previous Codex context was not found; a new context was started.",
                     "ru": "Прежний контекст Codex не найден; начат новый контекст.",
                     "uk": "Попередній контекст Codex не знайдено; почато новий контекст.",
                     "kk": "Codex-тің бұрынғы контексі табылмады; жаңа контекст басталды.",
                     "de": "Der bisherige Codex-Kontext wurde nicht gefunden; ein neuer Kontext wurde begonnen."},
    "empty_question": {"en": "Empty question.", "ru": "Пустой вопрос.", "uk": "Порожнє запитання.",
                       "kk": "Бос сұрақ.", "de": "Leere Frage."},
    "chat_context": {"en": "Context of the current chat:", "ru": "Контекст текущего чата:",
                     "uk": "Контекст поточного чату:", "kk": "Ағымдағы чаттың контексі:",
                     "de": "Kontext des aktuellen Chats:"},
    "user_request": {"en": "User's request:", "ru": "Запрос пользователя:", "uk": "Запит користувача:",
                     "kk": "Пайдаланушының сұрауы:", "de": "Anfrage des Benutzers:"},
    "timeout_stopped": {"en": "Codex did not finish the request in time and was stopped.",
                        "ru": "Codex не завершил запрос за отведённое время и был остановлен.",
                        "uk": "Codex не завершив запит вчасно і був зупинений.",
                        "kk": "Codex сұрауды белгіленген уақытта аяқтамады және тоқтатылды.",
                        "de": "Codex hat die Anfrage nicht rechtzeitig beendet und wurde gestoppt."},
    "timeout_restarted": {"en": "Codex did not finish the request in time; the app-server was restarted.",
                          "ru": "Codex не завершил запрос за отведённое время; app-server перезапущен.",
                          "uk": "Codex не завершив запит вчасно; app-server перезапущено.",
                          "kk": "Codex сұрауды белгіленген уақытта аяқтамады; app-server қайта іске қосылды.",
                          "de": "Codex hat die Anfrage nicht rechtzeitig beendet; der App-Server wurde neu gestartet."},
    "no_text": {"en": "(Codex returned no answer text)", "ru": "(Codex не вернул текста ответа)",
                "uk": "(Codex не повернув тексту відповіді)", "kk": "(Codex жауап мәтінін қайтармады)",
                "de": "(Codex hat keinen Antworttext geliefert)"},
    "codex_failed": {"en": "⚠️ Codex error: {error}", "ru": "⚠️ Ошибка Codex: {error}",
                     "uk": "⚠️ Помилка Codex: {error}", "kk": "⚠️ Codex қатесі: {error}",
                     "de": "⚠️ Codex-Fehler: {error}"},
    "server_closed": {"en": "app-server closed", "ru": "app-server закрыт", "uk": "app-server закрито",
                      "kk": "app-server жабылды", "de": "App-Server geschlossen"},
    "session_reset": {"en": "the session was reset", "ru": "сессия сброшена", "uk": "сесію скинуто",
                      "kk": "сессия қалпына келтірілді", "de": "die Sitzung wurde zurückgesetzt"},
    "restarted": {"en": "⚠️ The request was interrupted by a worker restart; the action was not repeated.",
                  "ru": "⚠️ Запрос прерван перезапуском worker; действие не повторялось.",
                  "uk": "⚠️ Запит перервано перезапуском worker; дію не повторено.",
                  "kk": "⚠️ Сұрау worker қайта іске қосылғандықтан үзілді; әрекет қайталанбады.",
                  "de": "⚠️ Die Anfrage wurde durch einen Neustart des Workers unterbrochen; die Aktion wurde nicht wiederholt."},
    "queue_error": {"en": "⚠️ Queue error: {error}", "ru": "⚠️ Ошибка очереди: {error}",
                    "uk": "⚠️ Помилка черги: {error}", "kk": "⚠️ Кезек қатесі: {error}",
                    "de": "⚠️ Warteschlangenfehler: {error}"},
    "fallback_persona": {
        "en": "You are Jarvis, a cheeky AI agent with a character of your own. Reply in the language "
              "of the message you are answering, in simple HTML. Never call yourself a language model "
              "and never mention the companies or models you run on.",
        "ru": "Ты — Jarvis, дерзкий ИИ-агент с собственным характером. Отвечай на языке сообщения, "
              "на которое отвечаешь, простым HTML. Никогда не называй себя языковой моделью и не "
              "упоминай компании или модели, на которых работаешь.",
        "uk": "Ти — Jarvis, зухвалий ШІ-агент із власним характером. Відповідай мовою повідомлення, "
              "на яке відповідаєш, простим HTML. Ніколи не називай себе мовною моделлю і не згадуй "
              "компанії чи моделі, на яких працюєш.",
        "kk": "Сен — Jarvis, өз мінезі бар батыл ЖИ-агентсің. Жауап беріп отырған хабарламаның "
              "тілінде, қарапайым HTML-мен жауап бер. Өзіңді ешқашан тілдік модель деп атама және "
              "қандай компанияның не модельдің негізінде жұмыс істейтініңді айтпа.",
        "de": "Du bist Jarvis, ein frecher KI-Agent mit eigenem Charakter. Antworte in der Sprache der "
              "Nachricht, auf die du antwortest, in einfachem HTML. Bezeichne dich nie als Sprachmodell "
              "und nenne nie die Firmen oder Modelle, auf denen du läufst.",
    },
    "no_mats": {
        "en": "\n\nIMPORTANT: profanity and obscene language are STRICTLY forbidden in this chat. "
              "Answer politely and courteously.",
        "ru": "\n\nВНИМАНИЕ: в этом чате КАТЕГОРИЧЕСКИ запрещён мат и нецензурная лексика. "
              "Отвечай вежливо и культурно.",
        "uk": "\n\nУВАГА: у цьому чаті КАТЕГОРИЧНО заборонені лайка та нецензурна лексика. "
              "Відповідай ввічливо й культурно.",
        "kk": "\n\nНАЗАР АУДАР: бұл чатта балағат пен әдепсіз сөздерге ҚАТАҢ тыйым салынған. "
              "Сыпайы және мәдениетті жауап бер.",
        "de": "\n\nACHTUNG: In diesem Chat sind Schimpfwörter und obszöne Sprache STRENG verboten. "
              "Antworte höflich und kultiviert.",
    },
}


def _language(value):
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def _text(key, language=None, **values):
    text = _TEXTS[key].get(_language(language)) or _TEXTS[key]["en"]
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text
POLL_INTERVAL = float(os.environ.get("CODEX_JARVIS_POLL_INTERVAL", "0.35"))
TURN_TIMEOUT = float(os.environ.get("CODEX_JARVIS_TURN_TIMEOUT", "1800"))
TURN_INTERRUPT_TIMEOUT = float(os.environ.get("CODEX_JARVIS_TURN_INTERRUPT_TIMEOUT", "15"))
PROGRESS_THROTTLE = float(os.environ.get("CODEX_JARVIS_PROGRESS_THROTTLE", "1.0"))
WORKER_QUEUE_MAX = int(os.environ.get("CODEX_JARVIS_WORKER_QUEUE_MAX", "64"))
WORKER_CONCURRENCY = int(os.environ.get("CODEX_JARVIS_WORKER_CONCURRENCY", "4"))
SESSION_IDLE_S = float(os.environ.get("CODEX_JARVIS_SESSION_IDLE_S", "3600"))

# Ported verbatim from claude-jarvis/claude_watcher.py's BASE_PERSONA (the
# ClaudeAsk persona) so CodexAsk isn't a differently-branded, thinner
# assistant with no idea what it is -- the two run on the same trigger DB,
# the same MCP telegram_actions server and the same tool contract, so the
# identity/tool/trigger sections apply unchanged. Only the invoking command
# differs (.xask, not .ask). Keep the two in sync by hand if either changes.
# ---------------------------------------------------------------------------
# Persona loading. The chat persona is not baked into this file: it lives in
# an editable, git-ignored Markdown file so it can be viewed and rewritten at
# runtime (see the `.xpersona` userbot command and the /xpersona endpoint in
# cmd_queue.py) with no code change and no restart.
#
# Lookup order per instance_id:
#   1. $JARVIS_PERSONA_DIR/<instance_id>.md   this instance's own persona
#   2. $JARVIS_PERSONA_DIR/default.md         shared local override
#   3. personas/default.md.example            neutral template shipped in the
#      repo; {{OWNER_NAME}}/{{OWNER_TG_ID}} are filled from env
# Re-read whenever the file mtime changes, so an edit lands on the next
# .xask with no watcher restart.
# ---------------------------------------------------------------------------
PERSONA_DIR = os.environ.get(
    "JARVIS_PERSONA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas"),
)
_PERSONA_TEMPLATE = os.path.join(PERSONA_DIR, "default.md.example")
_PERSONA_OWNER_NAME = os.environ.get("JARVIS_OWNER_NAME", "владелец")
_PERSONA_OWNER_TG_ID = os.environ.get("JARVIS_OWNER_TG_ID", "")
_PERSONA_FALLBACK = _TEXTS["fallback_persona"][DEFAULT_LANGUAGE]
_persona_cache: dict = {}  # (instance_id, language) -> (path, mtime, text)
_persona_lock = threading.Lock()


def _fill_persona_placeholders(text: str) -> str:
    return (text.replace("{{OWNER_NAME}}", _PERSONA_OWNER_NAME)
                .replace("{{OWNER_TG_ID}}", _PERSONA_OWNER_TG_ID))


def persona_file(instance_id: str) -> str:
    """Path to this instance's editable persona file (may not exist yet)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id or DEFAULT_INSTANCE)
    return os.path.join(PERSONA_DIR, safe + ".md")


_PERSONA_READ_CAP = 256 * 1024  # generous; a real persona is a few KB


def _read_persona_file(path: str) -> str:
    """Read a persona file without following a symlink -- it must be a regular
    file, never a pointer elsewhere. Capped at _PERSONA_READ_CAP."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read(_PERSONA_READ_CAP + 1)


def _persona_template(language: str) -> str:
    """The shipped template for a language: default.<lang>.md.example, else
    default.md.example (English)."""
    if language != "en":
        localized = os.path.join(PERSONA_DIR, f"default.{language}.md.example")
        if os.path.exists(localized):
            return localized
    return _PERSONA_TEMPLATE


def load_persona(instance_id: "str | None" = None, language: "str | None" = None) -> str:
    instance_id = instance_id or DEFAULT_INSTANCE
    language = _language(language)
    cache_key = (instance_id, language)
    for path in (persona_file(instance_id),
                 os.path.join(PERSONA_DIR, "default.md"),
                 _persona_template(language)):
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        with _persona_lock:
            cached = _persona_cache.get(cache_key)
            if cached and cached[0] == path and cached[1] == stamp:
                return cached[2]
        try:
            raw = _read_persona_file(path)
        except (OSError, UnicodeError):
            continue
        if len(raw) > _PERSONA_READ_CAP:
            continue  # implausibly large / not a real persona file
        # {{OWNER_*}} placeholders are filled for every source, not just the
        # bundled template -- a hand-written persona that keeps them still works.
        text = _fill_persona_placeholders(raw).strip()
        if not text:
            continue  # empty / whitespace-only -> fall through to the next source
        with _persona_lock:
            _persona_cache[cache_key] = (path, stamp, text)
        return text
    return _text("fallback_persona", language)


# Appended to the system prompt for chats listed in $JARVIS_NOMATS_CHAT_IDS
# (comma-separated Telegram chat ids, set by setup.sh / jarvis.env) so the
# persona's default profanity is suppressed there. Empty by default.
NO_MATS_RULE = _TEXTS["no_mats"][DEFAULT_LANGUAGE]
NOMATS_CHAT_IDS = {
    c.strip()
    for c in os.environ.get("JARVIS_NOMATS_CHAT_IDS", "").split(",")
    if c.strip()
}


def log(message: str) -> None:
    print(f"[codex-jarvis] {message}", flush=True)


# Same scheme as cmd_queue.py's _artifact_path/_register_artifact (not
# imported -- the two live in separate processes/services, communicating
# through this shared /tmp state the same way RESULT_DIR etc. already do).
# A generated-image path is useless to the userbot's /download fetch
# without this: cmd_queue.py's /download 404s on anything that isn't
# registered first (the S02 TODO.md finding's fix -- /download used to
# accept the whole filesystem, now it only serves artifacts an instance
# explicitly vouched for). The existing explicit send_file tool call never
# registered anything either; this closes that gap for both paths, not
# just the new automatic one.
RELAY_STATE_DIR = str(_shared_state_dir("JARVIS_RELAY_STATE_DIR", "relay_state"))


def _register_artifact_for_download(instance_id: str, path: str) -> bool:
    resolved = os.path.realpath(path)
    if not os.path.isabs(resolved) or not os.path.isfile(resolved):
        return False
    key = hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()
    target = Path(RELAY_STATE_DIR) / "artifacts" / instance_id / f"{key}.json"
    _atomic_json(target, {"path": resolved})
    return True


def ensure_dirs() -> None:
    for path in (QUEUE_DIR, RESULT_DIR, RESET_DIR, TOOL_CONTEXT_DIR, STATE_DIR, CODEX_HOME):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


# The relay drops "<request_id>.cancel" into the queue directory when the
# userbot asks to stop a request this worker has already claimed.
CANCEL_MARKER_TTL_S = 600


def _cancel_marker(req_id: str) -> Path:
    return QUEUE_DIR / f"{req_id}.cancel"


def _cancelled_result(req_id: str) -> dict:
    return {"done": True, "request_id": req_id, "cancelled": True, "answer": "", "thoughts": []}


def _tool_context_path(instance_id: str, chat_id: str) -> Path:
    key = f"{instance_id}\0{chat_id}".encode("utf-8")
    return TOOL_CONTEXT_DIR / f"{hashlib.sha256(key).hexdigest()}.json"


class SessionIndex:
    """Small persistent ``(instance, chat) -> app-server thread`` index."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        try:
            with path.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.data = loaded if isinstance(loaded, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            self.data = {}

    @staticmethod
    def key(instance_id: str, chat_id: str) -> str:
        return f"{instance_id}:{chat_id}"

    def get(self, instance_id: str, chat_id: str) -> str | None:
        with self.lock:
            value = self.data.get(self.key(instance_id, chat_id))
            return value if isinstance(value, str) and value else None

    def set(self, instance_id: str, chat_id: str, thread_id: str) -> None:
        with self.lock:
            self.data[self.key(instance_id, chat_id)] = thread_id
            _atomic_json(self.path, self.data)

    def remove(self, instance_id: str, chat_id: str) -> None:
        with self.lock:
            self.data.pop(self.key(instance_id, chat_id), None)
            _atomic_json(self.path, self.data)


def _normalize_item(item: object) -> dict:
    if not isinstance(item, dict):
        return {"type": "unknown", "value": item}
    result = dict(item)
    result["type"] = {
        "agentMessage": "agent_message",
        "reasoning": "reasoning",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "webSearch": "web_search",
        "imageView": "image_view",
        "imageGeneration": "image_generation",
        "collabAgentToolCall": "collab_agent_tool_call",
        "subAgentActivity": "sub_agent_activity",
        "contextCompaction": "context_compaction",
        "enteredReviewMode": "entered_review_mode",
        "exitedReviewMode": "exited_review_mode",
        "plan": "plan",
        "sleep": "sleep",
    }.get(result.get("type"), result.get("type", "unknown"))
    # Keep the names used by the app-server protocol and add the snake_case
    # aliases used by the renderer.  This lets completed and in-progress
    # events use exactly the same human-facing formatting.
    if "aggregated_output" not in result:
        result["aggregated_output"] = result.get("aggregatedOutput")
    if "exit_code" not in result:
        result["exit_code"] = result.get("exitCode")
    return result


def _short(value: object, limit: int = 1200) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except Exception:
            text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _item_text(value: object) -> str:
    """Turn protocol text/list parts into a compact displayable string."""
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict):
                part = part.get("text") or part.get("content") or part.get("summary") or ""
            if part not in (None, ""):
                parts.append(str(part))
        return "\n".join(parts)
    if isinstance(value, dict):
        return _short(value)
    return str(value)


def _item_label(item: dict, language: "str | None" = None) -> tuple[str, str]:
    """Return a Claude-style (label, input) pair for one app-server item.

    The old renderer used generic fallbacks ("Выполняю", "выполняется" and
    "Действие Codex").  Those hide what Codex is actually doing and caused a
    fake status line to replace the initial spinner before any real event had
    arrived.  Every known tool now gets a concrete label; unknown future
    items degrade to a neutral "Инструмент" label without pretending that a
    command is running.
    """
    kind = item.get("type", "unknown")
    if kind == "command_execution":
        return "🔧 Bash", _short(item.get("command") or item.get("commandLine") or "")
    if kind == "file_change":
        changes = item.get("changes") or []
        paths = []
        for change in changes if isinstance(changes, list) else [changes]:
            if isinstance(change, dict) and change.get("path"):
                change_kind = change.get("kind")
                if isinstance(change_kind, dict):
                    change_kind = change_kind.get("type")
                suffix = _text({"add": "file_created", "delete": "file_deleted"}.get(
                    str(change_kind or ""), "file_changed"
                ), language)
                paths.append(f"{change['path']} — {suffix}")
        return _text("file_change", language), _short("\n".join(paths) or item.get("path") or "")
    if kind == "web_search":
        action = item.get("action") or {}
        if isinstance(action, dict):
            query = action.get("query") or action.get("url")
        else:
            query = None
        query = item.get("query") or query or ""
        return "🔍 WebSearch", _short(query)
    if kind == "mcp_tool_call":
        name = ".".join(filter(None, (item.get("server"), item.get("tool")))) or "Telegram tool"
        return f"🔧 {name}", _short(item.get("arguments") or "")
    if kind == "dynamic_tool_call":
        name = ".".join(filter(None, (item.get("namespace"), item.get("tool")))) or _text("tool", language)
        return f"🔧 {name}", _short(item.get("arguments") or "")
    if kind in ("collab_agent_tool_call", "sub_agent_activity"):
        name = item.get("tool") or item.get("kind") or _text("agent", language)
        content = item.get("prompt") or item.get("agentsStates") or item.get("status") or ""
        return _text("agent_label", language, name=name), _short(content)
    if kind == "image_view":
        return _text("image_view", language), _short(item.get("path") or "")
    if kind == "image_generation":
        return _text("image_generation", language), _short(item.get("failure") or "")
    if kind == "context_compaction":
        return _text("compaction", language), _text("compacted", language)
    if kind == "plan":
        return _text("plan", language), _short(item.get("text") or "")
    if kind == "sleep":
        duration = item.get("durationMs")
        try:
            duration = _text("seconds", language, value=f"{float(duration) / 1000:g}")
        except (TypeError, ValueError):
            duration = ""
        return _text("waiting", language), duration
    if kind in ("entered_review_mode", "exited_review_mode"):
        return _text("review", language), _text("review_on" if kind.startswith("entered") else "review_off", language)
    if kind in ("agent_message", "reasoning"):
        return "✍️", _item_text(item.get("text") or item.get("summary") or item.get("content"))

    # Future protocol types: expose only a short, useful value.  Never dump
    # the full event envelope (it may contain opaque IDs or large payloads).
    value = item.get("name") or item.get("tool") or item.get("query") or item.get("arguments")
    if value in (None, ""):
        value = str(kind).replace("_", " ")
    return "🔧 " + _text("tool", language), _short(value)


def _item_result_blocks(item: dict, language: "str | None" = None) -> list[tuple[str, str]]:
    """Render completed tool output like Claude's separate result blocks."""
    kind = item.get("type", "unknown")
    blocks: list[tuple[str, str]] = []
    if kind == "command_execution":
        output = item.get("aggregated_output") or item.get("aggregatedOutput")
        if output not in (None, ""):
            blocks.append(("✅ StdOut", _short(output, 1800)))
        exit_code = item.get("exit_code") if "exit_code" in item else item.get("exitCode")
        if exit_code is not None:
            try:
                ok = int(exit_code) == 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                blocks.append(("❌ StdErr", f"Exit code {exit_code}"))
    elif kind == "mcp_tool_call":
        if item.get("error"):
            error = item.get("error")
            blocks.append((_text("error", language), _short(error, 1200)))
        elif item.get("result") is not None:
            result = item.get("result")
            if isinstance(result, dict):
                content = result.get("content") or result.get("contentItems") or []
                result = _item_text(content) or result.get("structuredContent") or _text("result_received", language)
            if _is_internal_tool_result(result):
                # The result remains in the app-server conversation for Codex;
                # suppress only the duplicate internal diagnostic in Telegram
                # progress so it cannot cover the final answer.
                return blocks
            blocks.append(("✅ " + _text("result", language), _short(result, 1600)))
    elif kind == "dynamic_tool_call" and item.get("contentItems") is not None:
        blocks.append(("📤 " + _text("result", language), _short(_item_text(item.get("contentItems")), 1600)))
    return blocks


class TurnState:
    def __init__(self, request_id: str, generation: int = 0, language: "str | None" = None):
        self.request_id = request_id
        self.language = _language(language)
        self.generation = generation
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.pending_notifications: list[tuple[str, dict]] = []
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.items: list[dict] = []
        self.generated_image_paths: list[str] = []
        self.reasoning: list[str] = []
        self.final_text = ""
        self.stream_text = ""
        self.reasoning_text = ""
        self.current_item: dict | None = None
        self.active_item_id: str | None = None
        self.error: str | None = None
        # Set by ChatSession.cancel() (.xstop): the turn is to be interrupted
        # and reported as cancelled, never as an answer or an error.
        self.cancelled = False
        self.last_progress_at = 0.0
        self.last_progress = ""

    def _progress_value(self) -> str:
        with self.lock:
            lines: list[str] = []
            # stream_text (item/agentMessage/delta) is deliberately excluded
            # here. Unlike Claude Code's JSONL, which never streams the final
            # answer token-by-token, Codex's app-server does -- showing it
            # live turned every reply into a fake "typing" animation via
            # rapid-fire edits (the owner explicitly asked for this removed).
            # The final answer is still delivered whole and unaffected via
            # `answer()`/`final_text`, set independently at item/completed.
            thought = self.reasoning_text or (self.reasoning[-1] if self.reasoning else "")
            if thought:
                # Same neutral writing marker as Claude's renderer.  The
                # final answer may be preceded by a tool call, so calling it
                # "reasoning" (🤔) here is misleading and caused the abrupt
                # spinner → 🤔 transition reported by the owner.
                lines.append(f"✍️ {html.escape(thought[-3500:], quote=False)}")
            item = self.current_item
            if item and item.get("type") not in ("agent_message", "reasoning"):
                label, content = _item_label(item, self.language)
                lines.append(f"{html.escape(label, quote=False)}:")
                if content:
                    lines.append(f"<pre>{html.escape(content, quote=False)}</pre>")
                for result_label, result_content in _item_result_blocks(item, self.language):
                    lines.append(f"{html.escape(result_label, quote=False)}:")
                    lines.append(f"<pre>{html.escape(result_content, quote=False)}</pre>")
            return "\n".join(lines)

    def progress(self) -> str:
        with self.lock:
            now = time.monotonic()
            value = self._progress_value()
            # Do not publish a synthetic "🤔 Думаю" event for every protocol
            # notification.  The Telegram-side module owns the initial
            # spinner; this backend should publish only real thought/tool
            # content, exactly like Claude's watcher.
            if not value:
                return ""
            if value == self.last_progress and now - self.last_progress_at < PROGRESS_THROTTLE:
                return ""
            self.last_progress = value
            self.last_progress_at = now
            return value

    def add_notification(self, method: str, params: dict) -> None:
        with self.lock:
            if method == "item/agentMessage/delta":
                item_id = params.get("itemId")
                if item_id and item_id != self.active_item_id:
                    self.stream_text = ""
                self.active_item_id = item_id or self.active_item_id
                self.current_item = None
                self.stream_text += str(params.get("delta") or "")
            elif method in (
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            ):
                item_id = params.get("itemId")
                if item_id and item_id != self.active_item_id:
                    self.reasoning_text = ""
                self.active_item_id = item_id or self.active_item_id
                self.current_item = None
                self.reasoning_text += str(params.get("delta") or "")
            elif method == "item/commandExecution/outputDelta":
                item_id = params.get("itemId")
                if self.current_item and (
                    not item_id or self.current_item.get("id") == item_id
                ):
                    output = self.current_item.get("aggregated_output") or self.current_item.get("aggregatedOutput") or ""
                    self.current_item["aggregated_output"] = f"{output}{params.get('delta') or ''}"
            elif method == "item/mcpToolCall/progress":
                if self.current_item:
                    self.current_item["progress"] = params.get("message") or ""
            elif method in ("item/started", "item/updated", "item/completed"):
                item = _normalize_item(params.get("item"))
                item_type = item.get("type")
                # Protocol bookkeeping is not a model action.  In particular,
                # rendering userMessage here was another path to the generic
                # "Действие Codex" line before the real assistant event.
                if item_type in ("userMessage", "user_message", "hookPrompt", "hook_prompt"):
                    return
                item_id = item.get("id")
                if method in ("item/started", "item/updated"):
                    if item_type in ("agent_message", "reasoning"):
                        if item_id and item_id != self.active_item_id:
                            self.stream_text = ""
                            self.reasoning_text = ""
                        self.active_item_id = item_id or self.active_item_id
                        self.current_item = None
                    else:
                        # A tool call closes the preceding visible thought,
                        # just as in Claude's stream renderer.
                        if self.stream_text.strip():
                            self.reasoning.append(self.stream_text.strip())
                        if self.reasoning_text.strip():
                            self.reasoning.append(self.reasoning_text.strip())
                        self.stream_text = ""
                        self.reasoning_text = ""
                        self.current_item = item
                        self.active_item_id = item_id or self.active_item_id
                if method == "item/completed":
                    self.items.append(item)
                    if item_type == "image_generation":
                        # Mirrors codex-telegram-bot's TurnView (e4fd589):
                        # the app-server calls this savedPath, accept
                        # snake_case too for compatibility with older
                        # servers. The owner had to explicitly ask the
                        # model to send_file a generated image every time
                        # before this -- deliver it unprompted instead.
                        image_path = item.get("savedPath") or item.get("saved_path")
                        if isinstance(image_path, str) and image_path not in self.generated_image_paths:
                            self.generated_image_paths.append(image_path)
                    if item_type == "agent_message":
                        text = _item_text(item.get("text")) or self.stream_text
                        self.final_text = text or self.final_text
                        self.stream_text = ""
                        self.current_item = None
                    elif item_type == "reasoning":
                        text = _item_text(item.get("text") or item.get("summary") or item.get("content"))
                        text = text or self.reasoning_text
                        if text:
                            self.reasoning.append(text)
                        self.reasoning_text = ""
                        self.current_item = None
                    else:
                        self.current_item = item
                    self.active_item_id = item_id or self.active_item_id
            elif method == "error" and not params.get("willRetry"):
                error = params.get("error")
                if isinstance(error, dict):
                    info = error.get("codexErrorInfo")
                    message = error.get("message") or str(error)
                    self.error = _friendly_error(info, message, self.language)
                else:
                    self.error = _friendly_error(None, str(error or _text("codex_error", self.language)), self.language)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if isinstance(turn, dict) and turn.get("error"):
                    error = turn["error"]
                    if isinstance(error, dict):
                        self.error = _friendly_error(error.get("codexErrorInfo"), error.get("message", str(error)), self.language)
                    else:
                        self.error = _friendly_error(None, str(error), self.language)
                self.done.set()

    def answer(self) -> str:
        with self.lock:
            if self.final_text:
                return self.final_text
            return self.stream_text


def _friendly_error(info: object, message: object, language: "str | None" = None) -> str:
    text = str(message or _text("codex_error", language))
    lowered = f"{info or ''} {text}".lower()
    if "usagelimitexceeded" in lowered:
        return _text("usage_limit", language)
    if "sessionbudgetexceeded" in lowered:
        return _text("session_budget", language)
    if "contextwindowexceeded" in lowered or "context window" in lowered:
        return _text("context_full", language)
    return _short(text, 1800)


class ChatSession:
    def __init__(self, instance_id: str, chat_id: str, index: SessionIndex):
        self.instance_id = instance_id
        self.chat_id = chat_id
        self.index = index
        self.lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self.active: TurnState | None = None
        self.generation = 0
        self.resetting = False
        self.new_context_notice = ""
        self.last_used = time.monotonic()
        self.thread_id = index.get(instance_id, chat_id)
        self.tool_context_path = _tool_context_path(instance_id, chat_id)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(CODEX_HOME)
        # Kept on the app-server's OWN process env for reference, but this is
        # NOT what actually reaches telegram_actions_mcp.py: Codex does not
        # forward arbitrary custom env vars from the app-server process to
        # the MCP server subprocesses it spawns per mcp_servers config
        # (confirmed live 2026-08-30 -- CHAT_ID arrived as "" in the MCP
        # server no matter what was set here, breaking every chat/instance-
        # scoped tool call with "invalid literal for int() with base 10:
        # ''"). What Codex DOES honor is an explicit env table on the
        # mcp_servers.<name> config entry itself, passed below as a -c
        # override on the app-server invocation -- see AppServerClient's
        # extra_args and codex app-server --help's own -c documentation.
        env["CODEX_TELEGRAM_CHAT_ID"] = str(chat_id)
        env["CODEX_TELEGRAM_INSTANCE_ID"] = str(instance_id)
        env["CODEX_TELEGRAM_CONTEXT_DIR"] = str(TOOL_CONTEXT_DIR)

        def _toml_str(value):
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        # Same "-c mcp_servers.<name>.env=" override as the three vars
        # above, and for the same reason (Codex does not forward the
        # app-server's own process env to spawned MCP servers -- see the
        # comment above). telegram_actions_mcp.py needs its own relay
        # bearer token (JARVIS_RELAY_TOKEN, or JARVIS_RELAY_TOKENS_JSON
        # when this env variable is set to serve several instances) to
        # authenticate every /tool_call it makes; without it here the
        # relay's S01 auth rejects every tool call from this process,
        # regardless of what is set on the watcher's own environment.
        mcp_env_entries = {
            "CODEX_TELEGRAM_CHAT_ID": chat_id,
            "CODEX_TELEGRAM_INSTANCE_ID": instance_id,
            "CODEX_TELEGRAM_CONTEXT_DIR": TOOL_CONTEXT_DIR,
        }
        for key in ("JARVIS_RELAY_TOKEN", "JARVIS_RELAY_TOKENS_JSON", "JARVIS_RELAY_URL"):
            value = os.environ.get(key)
            if value:
                mcp_env_entries[key] = value
        mcp_env_override = "mcp_servers.telegram_actions.env={" + ",".join(
            f"{key}={_toml_str(value)}" for key, value in mcp_env_entries.items()
        ) + "}"
        self._client_env = env
        self._mcp_env_override = mcp_env_override
        self.client = self._new_client()

    def _new_client(self, restricted: bool = False):
        extra_args = ["-c", "mcp_servers={}"] if restricted else ["-c", self._mcp_env_override]
        return AppServerClient(
            self._notification, lambda msg: log(f"{self.instance_id}:{self.chat_id} {msg}"),
            env=self._client_env, extra_args=extra_args,
        )

    @staticmethod
    def _event_ids(params: dict) -> tuple[str | None, str | None]:
        turn = params.get("turn") or {}
        if not isinstance(turn, dict):
            turn = {}
        thread_id = params.get("threadId") or turn.get("threadId") or turn.get("thread_id")
        turn_id = params.get("turnId") or turn.get("id")
        return (str(thread_id) if thread_id else None, str(turn_id) if turn_id else None)

    def _notification(self, method: str, params: dict) -> None:
        with self.lock:
            active = self.active
        if active is None:
            return
        params = params or {}
        thread_id, turn_id = self._event_ids(params)
        if active.generation != self.generation:
            log(f"{self.instance_id}:{self.chat_id} ignored stale {method} after reset")
            return
        if active.thread_id and thread_id != active.thread_id:
            log(f"{self.instance_id}:{self.chat_id} ignored {method} for foreign/missing thread {thread_id!r}")
            return
        if active.turn_id is None:
            if turn_id:
                active.pending_notifications.append((method, params))
                log(f"{self.instance_id}:{self.chat_id} deferred {method} until turn id is confirmed")
            else:
                log(f"{self.instance_id}:{self.chat_id} ignored {method} without a turn id")
            return
        if turn_id != active.turn_id:
            log(f"{self.instance_id}:{self.chat_id} ignored {method} for foreign/missing turn {turn_id!r}")
            return
        if thread_id:
            active.thread_id = thread_id
        active.add_notification(method, params)
        progress = active.progress()
        if progress:
            _atomic_json(RESULT_DIR / f"{active.request_id}.json", {
                "done": False, "request_id": active.request_id, "progress": progress,
            })

    def _thread_params(self, thread_id: str | None = None, ephemeral: bool = False) -> dict:
        params = {
            "cwd": CODEX_CWD,
            "sandbox": CODEX_SANDBOX,
            "approvalPolicy": "never",
        }
        if thread_id:
            params["threadId"] = thread_id
        if ephemeral:
            # Kept in memory only: no rollout file in the sessions store.
            params["ephemeral"] = True
        return params

    @staticmethod
    def _missing_thread_error(exc: AppServerError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in (
            "thread not found", "no rollout found for thread id",
            "no rollout found for conversation id", "invalid thread id",
        ))

    def _is_current_generation(self, generation: int) -> bool:
        with self.lock:
            return generation == self.generation and not self.resetting

    def _ensure_thread(self, persistent: bool, client=None, generation: int | None = None,
                       ephemeral: bool = False, language: "str | None" = None) -> str:
        client = client or self.client
        generation = self.generation if generation is None else generation
        client.start_if_needed()
        requested = self.thread_id if persistent else None
        if requested:
            try:
                result = client.request("thread/resume", self._thread_params(requested), timeout=60) or {}
                thread = (result.get("thread") or {}).get("id")
                if thread:
                    return thread
                raise AppServerError("thread/resume returned no thread id")
            except AppServerError as exc:
                if not self._missing_thread_error(exc):
                    raise
                log(f"{self.instance_id}:{self.chat_id} thread missing; starting a new context")
                self.new_context_notice = _text("context_lost", language)
                self.thread_id = None
                self.index.remove(self.instance_id, self.chat_id)
        if not self._is_current_generation(generation):
            raise AppServerError("session was reset")
        result = client.request("thread/start", self._thread_params(ephemeral=ephemeral), timeout=60) or {}
        thread = (result.get("thread") or {}).get("id")
        if not thread:
            raise AppServerError("thread/start returned no thread id")
        if persistent:
            if not self._is_current_generation(generation):
                raise AppServerError("session was reset")
            self.thread_id = thread
            self.index.set(self.instance_id, self.chat_id, thread)
        return thread

    def handle(self, request: dict) -> None:
        # Codex app-server has one active turn per thread.  Queue files can
        # arrive concurrently for the same chat, so serialize the whole
        # request instead of letting turns overwrite ``self.active`` and
        # each other's tool-call context.
        with self.turn_lock:
            self.last_used = time.monotonic()
            self._handle(request)

    def _handle(self, request: dict) -> None:
        req_id = str(request.get("request_id") or uuid.uuid4())
        mode = str(request.get("mode") or "chat")
        language = _language(request.get("language"))
        question = str(request.get("question") or "").strip()
        if not question:
            _atomic_json(RESULT_DIR / f"{req_id}.json", {
                "done": True, "request_id": req_id, "answer": _text("empty_question", language), "thoughts": [],
            })
            return
        if _cancel_marker(req_id).exists():
            # Stopped while it was waiting for this chat's previous turn.
            _atomic_json(RESULT_DIR / f"{req_id}.json", _cancelled_result(req_id))
            _cancel_marker(req_id).unlink(missing_ok=True)
            return
        with self.lock:
            if self.resetting:
                raise AppServerError("session was reset")
            generation = self.generation
        state = TurnState(req_id, generation, language)
        with self.lock:
            self.active = state
        if mode == "chat":
            try:
                _atomic_json(self.tool_context_path, {
                    "request_id": req_id,
                    "requester_id": request.get("requester_id"),
                    "topic_id": request.get("topic_id"),
                    "message_id": request.get("message_id"),
                    "chat_id": str(request.get("chat_id") or self.chat_id),
                    "language": language,
                })
            except Exception as exc:
                log(f"{self.instance_id}:{self.chat_id} tool context unavailable: {exc}")
                try:
                    self.tool_context_path.unlink()
                except FileNotFoundError:
                    pass
        # Utility turns with no tools: the trigger classifier and the relay's
        # persona translation (cmd_queue.py, /xpersona_language), which also
        # leaves no session behind.
        restricted = mode in ("classify", "persona_translate")
        client = self.client if not restricted else self._new_client(restricted=True)
        try:
            persistent = mode == "chat"
            thread_id = self._ensure_thread(
                persistent, client, generation, ephemeral=mode == "persona_translate", language=language,
            )
            state.thread_id = thread_id
            if mode == "classify":
                prompt = f"{CLASSIFY_PROMPT}\n\n{question}"
                model = CODEX_CLASSIFY_MODEL
            elif mode == "persona_translate":
                prompt = question  # the relay's instructions and the persona
                model = CODEX_MODEL
            else:
                persona = load_persona(self.instance_id, language)
                if str(request.get("chat_id") or self.chat_id) in NOMATS_CHAT_IDS:
                    persona += _text("no_mats", language)
                chat_context = str(request.get("chat_context") or "").strip()
                context_prefix = (
                    f"\n\n{_text('chat_context', language)}\n{chat_context}" if chat_context else ""
                )
                prompt = f"{persona}{context_prefix}\n\n{_text('user_request', language)}\n{question}"
                model = CODEX_MODEL
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": CODEX_CWD,
                "approvalPolicy": "never",
                "model": model,
                "effort": CODEX_EFFORT,
                "sandboxPolicy": (
                    {"type": "readOnly", "networkAccess": False} if restricted else {
                        "type": "dangerFullAccess" if CODEX_SANDBOX == "danger-full-access" else (
                            "readOnly" if CODEX_SANDBOX == "read-only" else "workspaceWrite"
                        ),
                        **({"writableRoots": [CODEX_CWD], "networkAccess": True} if CODEX_SANDBOX == "workspace-write" else {}),
                    }
                ),
            }
            result = client.request("turn/start", params, timeout=60) or {}
            turn = result.get("turn") or {}
            if isinstance(turn, dict) and turn.get("id"):
                # The app-server events carry the authoritative completion;
                # keeping the id is useful in logs and future interrupt work.
                log(f"{self.instance_id}:{self.chat_id} turn {turn['id']} started")
                state.turn_id = str(turn["id"])
                deferred = state.pending_notifications
                state.pending_notifications = []
                for deferred_method, deferred_params in deferred:
                    self._notification(deferred_method, deferred_params)
            finished = state.done.wait(TURN_TIMEOUT)
            if state.cancelled:
                # .xstop: interrupt the turn for real (a turn left running
                # would keep spending the subscription and hold the thread).
                try:
                    client.request("turn/interrupt", {
                        "threadId": thread_id,
                        **({"turnId": state.turn_id} if state.turn_id else {}),
                    }, timeout=TURN_INTERRUPT_TIMEOUT)
                except Exception as exc:
                    log(f"{self.instance_id}:{self.chat_id} interrupt on stop failed: {exc}")
                    client.close()
                log(f"{self.instance_id}:{self.chat_id} request {req_id} stopped on request")
                _atomic_json(RESULT_DIR / f"{req_id}.json", _cancelled_result(req_id))
                return
            if not finished:
                try:
                    client.request("turn/interrupt", {
                        "threadId": thread_id,
                        **({"turnId": state.turn_id} if state.turn_id else {}),
                    }, timeout=TURN_INTERRUPT_TIMEOUT)
                    state.error = _text("timeout_stopped", language)
                except Exception as exc:
                    log(f"{self.instance_id}:{self.chat_id} interrupt failed: {exc}")
                    state.error = _text("timeout_restarted", language)
                    client.close()
            answer = state.answer()
            if state.error:
                answer = f"⚠️ {state.error}"
            if self.new_context_notice:
                answer = f"{self.new_context_notice}\n\n{answer}"
                self.new_context_notice = ""
            if not answer:
                answer = _text("no_text", language)
            for image_path in state.generated_image_paths:
                if not _register_artifact_for_download(self.instance_id, image_path):
                    log(f"{self.instance_id}:{self.chat_id} could not register generated image {image_path} for download")
            _atomic_json(RESULT_DIR / f"{req_id}.json", {
                "done": True,
                "request_id": req_id,
                "answer": answer,
                "thoughts": state.reasoning[-5:],
                "generated_image_paths": state.generated_image_paths,
            })
        except Exception as exc:
            if state.cancelled:
                # Stopped while the turn was still being set up: whatever that
                # setup then failed with is not what the user should be told.
                log(f"{self.instance_id}:{self.chat_id} request {req_id} stopped on request ({exc})")
                _atomic_json(RESULT_DIR / f"{req_id}.json", _cancelled_result(req_id))
            else:
                log(f"{self.instance_id}:{self.chat_id} request failed: {exc}")
                _atomic_json(RESULT_DIR / f"{req_id}.json", {
                    "done": True,
                    "request_id": req_id,
                    "answer": _text("codex_failed", language, error=_short(str(exc), 1800)),
                    "thoughts": [],
                })
        finally:
            with self.lock:
                if self.active is state:
                    self.active = None
            if restricted:
                client.close()
            _cancel_marker(req_id).unlink(missing_ok=True)
            try:
                with self.tool_context_path.open(encoding="utf-8") as handle:
                    context = json.load(handle)
                if context.get("request_id") == req_id:
                    self.tool_context_path.unlink()
            except (FileNotFoundError, OSError, ValueError, TypeError):
                pass

    def cancel(self, req_id: str) -> bool:
        """Stop the turn of one request if it is the active one."""
        with self.lock:
            state = self.active
            if state is None or state.request_id != req_id or state.cancelled:
                return False
            state.cancelled = True
        state.done.set()  # wake _handle, which interrupts and reports it
        return True

    def close(self) -> None:
        with self.lock:
            state = self.active
            if state is not None:
                state.error = state.error or _text("server_closed", state.language)
                state.done.set()
        self.client.close()

    def begin_reset(self) -> None:
        with self.lock:
            self.generation += 1
            self.resetting = True
            state = self.active
            if state is not None:
                state.error = _text("session_reset", state.language)
                state.done.set()
        self.client.close()

    def finish_reset(self) -> None:
        with self.lock:
            self.resetting = False


def _queued_language(path) -> "str | None":
    """The language a queued request asked for, when it can still be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("language")
    except Exception:
        return None


class Worker:
    def __init__(self):
        ensure_dirs()
        self.index = SessionIndex(SESSIONS_FILE)
        self.sessions: dict[str, ChatSession] = {}
        self.sessions_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pending = queue.Queue(maxsize=WORKER_QUEUE_MAX)
        self.admitted = set()
        self.admitted_lock = threading.Lock()
        self.workers = [threading.Thread(target=self._consume, daemon=True) for _ in range(WORKER_CONCURRENCY)]
        for thread in self.workers:
            thread.start()

    def admit(self, path: Path) -> bool:
        with self.admitted_lock:
            if path in self.admitted:
                return False
            try:
                self.pending.put_nowait(path)
            except queue.Full:
                return False
            self.admitted.add(path)
            return True

    def _consume(self):
        while not self.stop_event.is_set():
            try:
                path = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process_request(path)
            finally:
                with self.admitted_lock:
                    self.admitted.discard(path)
                self.pending.task_done()

    def recover_processing(self):
        for path in sorted(QUEUE_DIR.glob("*.json.processing")):
            req_id = path.name.split(".json.processing", 1)[0]
            _atomic_json(RESULT_DIR / f"{req_id}.json", {
                "done": True, "request_id": req_id, "thoughts": [],
                "answer": _text("restarted", _queued_language(path)),
            })
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _cleanup_idle_sessions(self):
        cutoff = time.monotonic() - SESSION_IDLE_S
        with self.sessions_lock:
            stale = [(key, session) for key, session in self.sessions.items()
                     if session.last_used < cutoff and not session.turn_lock.locked()]
            for key, _ in stale:
                self.sessions.pop(key, None)
        for _, session in stale:
            session.close()

    def _session(self, instance_id: str, chat_id: str) -> ChatSession:
        key = SessionIndex.key(instance_id, chat_id)
        with self.sessions_lock:
            session = self.sessions.get(key)
            if session is None:
                session = ChatSession(instance_id, chat_id, self.index)
                self.sessions[key] = session
            return session

    def _process_reset(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        instance_id = str(data.get("instance_id") or DEFAULT_INSTANCE)
        chat_id = str(data.get("chat_id") or "")
        if not chat_id:
            return
        key = SessionIndex.key(instance_id, chat_id)
        with self.sessions_lock:
            session = self.sessions.pop(key, None)
        if session:
            # Signal a blocked app-server wait before taking turn_lock; once
            # it exits, reset owns the lock and no stale worker can restore
            # the index or create/resume a thread after this point.
            session.begin_reset()
            with session.turn_lock:
                self.index.remove(instance_id, chat_id)
                session.finish_reset()
        else:
            self.index.remove(instance_id, chat_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        log(f"reset {instance_id}:{chat_id}")

    def _apply_cancel_markers(self, now: float | None = None) -> None:
        """Act on the relay's "<request_id>.cancel" markers. A request that
        has not started yet is handled by _handle itself when its turn comes;
        a marker nobody consumed is dropped after a while."""
        now = time.time() if now is None else now
        for marker in QUEUE_DIR.glob("*.cancel"):
            req_id = marker.name[:-len(".cancel")]
            with self.sessions_lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                if session.cancel(req_id):
                    break
            try:
                if now - marker.stat().st_mtime > CANCEL_MARKER_TTL_S:
                    marker.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

    def _process_request(self, path: Path) -> None:
        processing = path.with_suffix(path.suffix + ".processing")
        try:
            os.replace(path, processing)
        except FileNotFoundError:
            return
        try:
            with processing.open(encoding="utf-8") as handle:
                request = json.load(handle)
            instance_id = str(request.get("instance_id") or DEFAULT_INSTANCE)
            chat_id = str(request.get("chat_id") or "")
            if not chat_id:
                raise ValueError("queue item has no chat_id")
            session_chat_id = str(request.get("session_chat_id") or "")
            self._session(instance_id, session_chat_id or chat_id).handle(request)
        except Exception as exc:
            req_id = processing.stem.split(".", 1)[0]
            _atomic_json(RESULT_DIR / f"{req_id}.json", {
                "done": True, "request_id": req_id, "answer": _text("queue_error", _queued_language(processing), error=exc), "thoughts": [],
            })
        finally:
            try:
                processing.unlink()
            except FileNotFoundError:
                pass

    def run(self) -> None:
        log(f"started; queue={QUEUE_DIR} result={RESULT_DIR} codex_home={CODEX_HOME}")
        self.recover_processing()
        while not self.stop_event.is_set():
            try:
                for reset in sorted(RESET_DIR.glob("*.json")):
                    self._process_reset(reset)
                for path in sorted(QUEUE_DIR.glob("*.json")):
                    self.admit(path)
                self._apply_cancel_markers()
                self._cleanup_idle_sessions()
            except Exception as exc:
                log(f"poll error: {exc}")
            self.stop_event.wait(POLL_INTERVAL)

    def close(self) -> None:
        self.stop_event.set()
        with self.sessions_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()


def main() -> None:
    worker = Worker()
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()


if __name__ == "__main__":
    main()

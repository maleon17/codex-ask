#!/usr/bin/env bash
# Installer for the CodexAsk backend: the Codex worker (codex_ask_watcher.py),
# plus the shared pieces it needs from telegram-ai-core (queue relay,
# Telegram-action MCP server, userbot module) -- that repository is found next
# to this one or cloned automatically.
#
# Safe to re-run: existing jarvis.env keys and an existing CODEX_HOME config
# are kept, and a running worker is left running.
#
# Every question can be answered through the environment instead, which makes
# an unattended install possible:
#   TELEGRAM_AI_CORE_DIR      an existing telegram-ai-core checkout
#   CODEX_JARVIS_CWD, CODEX_JARVIS_HOME_DIR, CODEX_JARVIS_SERVICE
#   JARVIS_OWNER_NAME, JARVIS_OWNER_TG_ID, JARVIS_NOMATS_CHAT_IDS, JARVIS_PERSONA_DIR
#   JARVIS_INSTANCE_ID        instance to register with the relay
#   TELEGRAM_AI_CONFIG_DIR    credentials and registry (default ~/.config/telegram-ai)
#   JARVIS_STATE_DIR          queues and uploads       (default ~/.local/state/telegram-ai)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found"; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# ask VAR "prompt" [default] -- a value already in the environment wins, then
# an interactive answer, then the default.
ask() {
    local variable="$1" prompt="$2" fallback="${3:-}" value="${!1:-}"
    if [[ -z "$value" && -t 0 ]]; then read -r -p "$prompt" value || value=""; fi
    printf -v "$variable" '%s' "${value:-$fallback}"
}
# env_file_set FILE KEY VALUE -- replace one key, keep every other line.
env_file_set() {
    python3 - "$1" "$2" "$3" <<'PY'
import os, sys
path, key, value = sys.argv[1:4]
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
lines = [line for line in lines if not line.startswith(key + "=")]
lines.append(f"{key}={value}")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PY
}
env_file_get() { [[ -f "$1" ]] && sed -n "s/^$2=//p" "$1" | tail -n 1 || true; }
# The core checkout recorded in the component registry, if any.
registered_core() {
    python3 - <<'PY' 2>/dev/null || true
import json, os
base = os.environ.get("TELEGRAM_AI_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "telegram-ai")
with open(os.path.join(base, "registry.json"), encoding="utf-8") as handle:
    print(json.load(handle)["components"]["core"]["path"])
PY
}

echo "== Codex Jarvis setup =="
need python3
need systemctl
need codex
[[ $EUID -eq 0 ]] || need sudo

codex login status >/dev/null 2>&1 || die "Codex is not logged in; run: codex login"
echo "Codex account: authenticated"

# --- telegram-ai-core: relay, Telegram-action MCP server, userbot module -----
CORE_REPO_URL="${TELEGRAM_AI_CORE_REPO:-https://github.com/maleon17/telegram-ai-core.git}"
CORE_DIR=""
for candidate in "${TELEGRAM_AI_CORE_DIR:-}" "$(registered_core)" "$ROOT/../telegram-ai-core" "$HOME/telegram-ai-core"; do
    if [[ -n "$candidate" && -f "$candidate/install_relay.sh" && -f "$candidate/telegram_actions_mcp.py" ]]; then
        CORE_DIR="$(cd "$candidate" && pwd)"
        break
    fi
done
if [[ -z "$CORE_DIR" ]]; then
    need git
    CORE_DIR="$(dirname "$ROOT")/telegram-ai-core"
    echo "Fetching telegram-ai-core into $CORE_DIR"
    git clone --quiet --depth 1 "$CORE_REPO_URL" "$CORE_DIR" \
        || die "could not clone $CORE_REPO_URL (set TELEGRAM_AI_CORE_DIR to an existing checkout)"
fi
echo "telegram-ai-core: $CORE_DIR"

# Installs the relay's virtualenv and service if they are not there yet; a
# relay that is already running is left alone.
"$CORE_DIR/install_relay.sh"
CORE_PYTHON="$CORE_DIR/.venv/bin/python"
[[ -x "$CORE_PYTHON" ]] || die "telegram-ai-core has no virtualenv after install_relay.sh"
core_registry() { "$CORE_PYTHON" "$CORE_DIR/registry.py" "$@"; }

# The relay (installed above, possibly long ago) is the source of truth for
# where credentials and queues live.
RELAY_CONFIG_DIR="$(core_registry get relay config_dir)"
STATE_DIR="$(core_registry get relay state_dir)"
[[ -n "$RELAY_CONFIG_DIR" && -n "$STATE_DIR" ]] || die "the relay is not registered; run $CORE_DIR/install_relay.sh"
WORKER_TOKEN_ENV="$RELAY_CONFIG_DIR/relay-token-codex.env"

INSTALL_USER="$(id -un)"
INSTALL_DIR="$ROOT"
# A dedicated, initially empty directory by default: Codex obeys the AGENTS.md
# of its working directory, and one written for other work (for example in
# your home directory) would otherwise leak into every Jarvis answer.
ask CODEX_JARVIS_CWD "Codex workspace [$ROOT/workspace]: " "$ROOT/workspace"
CODEX_CWD="$CODEX_JARVIS_CWD"
[[ "$CODEX_CWD" == "$ROOT/workspace" ]] && mkdir -p "$CODEX_CWD"
[[ -d "$CODEX_CWD" ]] || die "workspace does not exist: $CODEX_CWD"
CODEX_CWD="$(cd "$CODEX_CWD" && pwd)"

ask CODEX_JARVIS_HOME_DIR "Dedicated CODEX_HOME [$ROOT/codex_home]: " "$ROOT/codex_home"
CODEX_HOME_DIR="$(mkdir -p "$CODEX_JARVIS_HOME_DIR" && cd "$CODEX_JARVIS_HOME_DIR" && pwd)"
chmod 700 "$CODEX_HOME_DIR"

AUTH_SOURCE="${CODEX_JARVIS_AUTH_FILE:-$HOME/.codex/auth.json}"
[[ -f "$AUTH_SOURCE" ]] || die "Codex auth file not found: $AUTH_SOURCE"
if [[ ! -e "$CODEX_HOME_DIR/auth.json" ]]; then
    ln -s "$AUTH_SOURCE" "$CODEX_HOME_DIR/auth.json"
fi

GENERATED_CONFIG="$(mktemp)"
sed \
    -e "s|__MCP_PYTHON__|$CORE_PYTHON|g" \
    -e "s|__CORE_DIR__|$CORE_DIR|g" \
    codex-config.toml.example > "$GENERATED_CONFIG"
if [[ ! -e "$CODEX_HOME_DIR/config.toml" ]]; then
    install -m 600 "$GENERATED_CONFIG" "$CODEX_HOME_DIR/config.toml"
elif ! cmp -s "$GENERATED_CONFIG" "$CODEX_HOME_DIR/config.toml"; then
    install -m 600 "$GENERATED_CONFIG" "$CODEX_HOME_DIR/config.toml.new"
    echo "Kept your existing $CODEX_HOME_DIR/config.toml; the freshly generated"
    echo "one is next to it as config.toml.new - merge it if this is an upgrade."
fi
rm -f "$GENERATED_CONFIG"

ask CODEX_JARVIS_SERVICE "systemd service name [codex-jarvis]: " codex-jarvis
SERVICE_NAME="${CODEX_JARVIS_SERVICE%.service}"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "invalid service name"

UNIT_FILE="$(mktemp)"
trap 'rm -f "$UNIT_FILE"' EXIT
sed \
    -e "s|__USER__|$INSTALL_USER|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__CODEX_HOME__|$CODEX_HOME_DIR|g" \
    -e "s|__CODEX_CWD__|$CODEX_CWD|g" \
    -e "s|__CONFIG_DIR__|$RELAY_CONFIG_DIR|g" \
    -e "s|__STATE_DIR__|$STATE_DIR|g" \
    codex-jarvis.service.example > "$UNIT_FILE"

# --- Jarvis persona: profanity notice + env (owner + softening list) ------
cat <<'NOTE'

Note: the Jarvis persona ships with profanity and dark humour enabled by
default - a deliberately informal, entertainment-leaning bot. To tone that
down, edit the persona after setup with the  .xpersona  command in Telegram
(ClaudeAsk: .persona), or edit personas/<instance>.md directly (re-read
automatically on change, no restart).
NOTE

_jarvis_clean() {  # drop control chars + shell/systemd-hazardous chars, keep UTF-8 letters
    python3 - "$1" <<'PY'
import sys
bad = set('\\"\'`$')
s = sys.argv[1] if len(sys.argv) > 1 else ""
sys.stdout.write("".join(c for c in s if c >= " " and c != "\x7f" and c not in bad))
PY
}

JARVIS_ENV="$ROOT/jarvis.env"
PREV_OWNER_NAME="$(env_file_get "$JARVIS_ENV" JARVIS_OWNER_NAME)"
PREV_OWNER_TG_ID="$(env_file_get "$JARVIS_ENV" JARVIS_OWNER_TG_ID)"
PREV_NOMATS="$(env_file_get "$JARVIS_ENV" JARVIS_NOMATS_CHAT_IDS)"
PREV_PERSONA_DIR="$(env_file_get "$JARVIS_ENV" JARVIS_PERSONA_DIR)"
ask JARVIS_OWNER_NAME "Owner display name for the persona template [${PREV_OWNER_NAME:-skip}]: " "$PREV_OWNER_NAME"
ask JARVIS_OWNER_TG_ID "Owner Telegram numeric id (message @userinfobot to get it) [${PREV_OWNER_TG_ID:-skip}]: " "$PREV_OWNER_TG_ID"
ask JARVIS_NOMATS_CHAT_IDS "Chat ids where profanity is suppressed, comma-separated [${PREV_NOMATS:-none}]: " "$PREV_NOMATS"
ask JARVIS_PERSONA_DIR "Persona directory [${PREV_PERSONA_DIR:-$ROOT/personas}]: " "${PREV_PERSONA_DIR:-$ROOT/personas}"
J_OWNER_NAME="$(_jarvis_clean "$JARVIS_OWNER_NAME")"
J_OWNER_TG_ID="$(printf '%s' "$JARVIS_OWNER_TG_ID" | tr -cd '0-9')"
J_NOMATS="$(printf '%s' "$JARVIS_NOMATS_CHAT_IDS" | tr -cd '0-9,-')"
J_PERSONA_DIR="$(_jarvis_clean "$JARVIS_PERSONA_DIR")"
if [[ "$J_PERSONA_DIR" != "$ROOT/personas" ]]; then
    mkdir -p "$J_PERSONA_DIR"
    [[ -f "$J_PERSONA_DIR/default.md.example" ]] || \
        cp "$ROOT/personas/default.md.example" "$J_PERSONA_DIR/default.md.example"
fi
[[ -n "$J_OWNER_NAME" ]]  && env_file_set "$JARVIS_ENV" JARVIS_OWNER_NAME "$J_OWNER_NAME"
[[ -n "$J_OWNER_TG_ID" ]] && env_file_set "$JARVIS_ENV" JARVIS_OWNER_TG_ID "$J_OWNER_TG_ID"
env_file_set "$JARVIS_ENV" JARVIS_NOMATS_CHAT_IDS "$J_NOMATS"
env_file_set "$JARVIS_ENV" JARVIS_PERSONA_DIR "$J_PERSONA_DIR"
chmod 600 "$JARVIS_ENV"
echo "Wrote $JARVIS_ENV (git-ignored; the worker loads it via EnvironmentFile=)."

# Lets the relay serve .xpersona from this checkout and the neighbours find it.
core_registry register codex-ask "path=$ROOT" "unit=$SERVICE_NAME.service" \
    "persona_dir=$J_PERSONA_DIR" "owner_name=$J_OWNER_NAME" "owner_tg_id=$J_OWNER_TG_ID"

WORKER_ACTIVE=0
systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null && WORKER_ACTIVE=1

echo
echo "== Relay authentication =="
RELAY_TOKEN=""
ask JARVIS_INSTANCE_ID "Instance ID for this CodexAsk deployment (e.g. mybot; blank to configure later): " ""
INSTANCE_ID="$JARVIS_INSTANCE_ID"
if [[ -n "$INSTANCE_ID" ]]; then
    [[ "$INSTANCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || die "invalid instance id: $INSTANCE_ID"
    ask JARVIS_INSTANCE_OWNER_TG_ID "Owner Telegram numeric ID for instance '$INSTANCE_ID'${J_OWNER_TG_ID:+ [$J_OWNER_TG_ID]}: " "$J_OWNER_TG_ID"
    OWNER_TG_ID="$(printf '%s' "$JARVIS_INSTANCE_OWNER_TG_ID" | tr -cd '0-9')"
    [[ -n "$OWNER_TG_ID" ]] || die "owner Telegram ID is required to authorize instance '$INSTANCE_ID'"

    RELAY_TOKEN="$("$CORE_DIR/register_instance.sh" "$INSTANCE_ID" "$OWNER_TG_ID" "$WORKER_TOKEN_ENV")"
    echo "Registered owner $OWNER_TG_ID and a new token for instance '$INSTANCE_ID' in $RELAY_CONFIG_DIR"
    echo "(a running relay picks this up by itself; no restart needed)."
    if (( WORKER_ACTIVE )); then
        echo "NOTE: ${SERVICE_NAME}.service is already running and reads $WORKER_TOKEN_ENV"
        echo "only at start. Restart it once no .xask is in flight:"
        echo "  sudo systemctl restart ${SERVICE_NAME}.service"
    fi
else
    echo "No instance ID given; skipping relay token generation."
    echo "Re-run setup.sh once you're ready, or see 'Relay authentication' in the"
    echo "telegram-ai-core README to do it by hand."
fi

python3 -m py_compile app_server.py codex_ask_watcher.py codex_ask.py jarvis_ask.py
if (( WORKER_ACTIVE )); then
    echo "${SERVICE_NAME}.service is already active; leaving it running."
else
    echo "Installing /etc/systemd/system/${SERVICE_NAME}.service (sudo required)"
    as_root install -m 0644 "$UNIT_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
    as_root systemctl daemon-reload
    as_root systemctl enable --now "${SERVICE_NAME}.service"
fi

echo
as_root systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo
if [[ -n "$INSTANCE_ID" && -n "$RELAY_TOKEN" ]]; then
    echo "Backend ready. Upload jarvis_ask.py then codex_ask.py to the Telethon userbot,"
    echo "reply .lm to each, then run (in a private chat - the token is shown once):"
    echo "  .xasknet token $RELAY_TOKEN"
    echo "  .xasknet local $INSTANCE_ID"
    echo "  .xasknet check"
else
    echo "Backend installed. Re-run setup.sh with an instance ID to register one, then"
    echo "upload jarvis_ask.py and codex_ask.py to the Telethon userbot, reply .lm to"
    echo "each and configure .xasknet."
fi

# --- optional: graphify code-map -------------------------------------------
# graphify (PyPI package "graphifyy", github.com/Graphify-Labs/graphify) turns
# this repo into a queryable knowledge graph under graphify-out/. Optional.
setup_graphify() {
    local platform="$1"
    if command -v graphify >/dev/null 2>&1; then
        echo "graphify: already on PATH ($(command -v graphify))"
    elif command -v uv >/dev/null 2>&1; then
        uv tool install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    elif command -v pipx >/dev/null 2>&1; then
        pipx install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    else
        echo "graphify: needs 'uv' or 'pipx' to install - skipping"
        return 0
    fi
    graphify install --platform "$platform" >/dev/null 2>&1 || true
    graphify update . >/dev/null 2>&1 || true
    echo "graphify: code map built under graphify-out/ (re-run 'graphify update .' after edits;"
    echo "          a post-commit hook keeps it fresh if graphify installed one)"
}

if [ -t 0 ]; then
    read -rp "Set up the graphify code-map for this repo? [y/N] " _SETUP_GRAPHIFY || _SETUP_GRAPHIFY=""
    case "${_SETUP_GRAPHIFY:-}" in
        [Yy]*) setup_graphify "codex" ;;
    esac
fi

bash "$CORE_DIR/scripts/setup_userbot_network.sh" codex_ask.py

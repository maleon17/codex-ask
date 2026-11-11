#!/usr/bin/env bash
# Installer for the CodexAsk backend worker (codex_ask_watcher.py).
#
# The worker shares queue files with the relay (cmd_queue.py from the ClaudeAsk
# checkout), so the relay must live on this same host.
#
# Safe to re-run: existing jarvis.env keys and an existing CODEX_HOME config
# are kept, and a running worker is left running.
#
# Every question can be answered through the environment instead, which makes
# an unattended install possible:
#   CODEX_JARVIS_CWD, CODEX_JARVIS_HOME_DIR, CODEX_JARVIS_SERVICE
#   CLAUDE_JARVIS_CHECKOUT    ClaudeAsk checkout that provides the relay
#   JARVIS_OWNER_NAME, JARVIS_OWNER_TG_ID, JARVIS_NOMATS_CHAT_IDS, JARVIS_PERSONA_DIR
#   JARVIS_INSTANCE_ID        instance to register with the relay
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
# The value of one variable in a running service's environment, if readable.
running_env() {
    local pid
    pid="$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)"
    [[ -n "$pid" && "$pid" != 0 && -r "/proc/$pid/environ" ]] || return 0
    tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$2=//p" | tail -n 1
}

echo "== Codex Jarvis setup =="
need python3
need systemctl
need codex
[[ $EUID -eq 0 ]] || need sudo
python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
    || die "python3 venv support is missing (Debian/Ubuntu: sudo apt install python3-venv)"

codex login status >/dev/null 2>&1 || die "Codex is not logged in; run: codex login"
echo "Codex account: authenticated"

# --- Shared relay ------------------------------------------------------------
# Checked before anything is installed, so a missing relay fails fast instead
# of leaving a worker that can never receive a request.
QUEUE_SERVICE_NAME="${JARVIS_RELAY_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"
RELAY_ACTIVE=0
systemctl is-active --quiet "$QUEUE_SERVICE_NAME" 2>/dev/null && RELAY_ACTIVE=1

DEFAULT_CLAUDE_JARVIS_DIR="${CLAUDE_JARVIS_CHECKOUT:-}"
if [[ -z "$DEFAULT_CLAUDE_JARVIS_DIR" ]]; then
    for candidate in "$ROOT/../claude-ask" "$ROOT/../claude-jarvis" "$HOME/claude-ask" "$HOME/claude-jarvis"; do
        if [[ -f "$candidate/cmd_queue.py" ]]; then
            DEFAULT_CLAUDE_JARVIS_DIR="$(cd "$candidate" && pwd)"
            break
        fi
    done
fi
ask CLAUDE_JARVIS_CHECKOUT "ClaudeAsk checkout that provides the relay [${DEFAULT_CLAUDE_JARVIS_DIR:-not found}]: " "$DEFAULT_CLAUDE_JARVIS_DIR"
CLAUDE_JARVIS_DIR="$CLAUDE_JARVIS_CHECKOUT"
RELAY_PYTHON="$CLAUDE_JARVIS_DIR/.venv/bin/python"
if [[ ! -f "$CLAUDE_JARVIS_DIR/cmd_queue.py" || ! -x "$RELAY_PYTHON" ]] || (( ! RELAY_ACTIVE )); then
    die "the shared queue relay is not installed on this host.
CodexAsk exchanges queue files with cmd_queue.py from the ClaudeAsk checkout,
so that relay has to be installed and running here first:
  git clone https://github.com/maleon17/claude-ask.git \"$(dirname "$ROOT")/claude-ask\"
  cd \"$(dirname "$ROOT")/claude-ask\" && ./setup.sh
Then re-run this script (CLAUDE_JARVIS_CHECKOUT=<path> if it lives elsewhere)."
fi

# The running relay is the source of truth for where its credentials and
# queues live; a token or queue item written anywhere else is invisible to it.
for name in JARVIS_RELAY_CONFIG_DIR JARVIS_RELAY_TOKENS_FILE JARVIS_RELAY_OWNERS_FILE JARVIS_STATE_DIR; do
    live_value="$(running_env "$QUEUE_SERVICE_NAME" "$name")"
    [[ -n "$live_value" ]] && export "$name=$live_value"
done
RELAY_CONFIG_DIR="${JARVIS_RELAY_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/telegram-ai}"
STATE_DIR="${JARVIS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/telegram-ai}"
export JARVIS_RELAY_CONFIG_DIR="$RELAY_CONFIG_DIR" JARVIS_STATE_DIR="$STATE_DIR"
WORKER_TOKEN_ENV="$RELAY_CONFIG_DIR/relay-token-codex.env"

INSTALL_USER="$(id -un)"
INSTALL_DIR="$ROOT"
ask CODEX_JARVIS_CWD "Codex workspace [$HOME]: " "$HOME"
CODEX_CWD="$CODEX_JARVIS_CWD"
[[ -d "$CODEX_CWD" ]] || die "workspace does not exist: $CODEX_CWD"
CODEX_CWD="$(cd "$CODEX_CWD" && pwd)"

ask CODEX_JARVIS_HOME_DIR "Dedicated CODEX_HOME [$ROOT/codex_home]: " "$ROOT/codex_home"
CODEX_HOME_DIR="$(mkdir -p "$CODEX_JARVIS_HOME_DIR" && cd "$CODEX_JARVIS_HOME_DIR" && pwd)"
chmod 700 "$CODEX_HOME_DIR"

MCP_VENV="${CODEX_JARVIS_MCP_VENV:-$ROOT/.venv}"
echo "Installing MCP dependencies into $MCP_VENV"
python3 -m venv "$MCP_VENV"
"$MCP_VENV/bin/python" -m pip install --quiet --upgrade pip
"$MCP_VENV/bin/python" -m pip install --quiet -r requirements-mcp.txt

AUTH_SOURCE="${CODEX_JARVIS_AUTH_FILE:-$HOME/.codex/auth.json}"
[[ -f "$AUTH_SOURCE" ]] || die "Codex auth file not found: $AUTH_SOURCE"
if [[ ! -e "$CODEX_HOME_DIR/auth.json" ]]; then
    ln -s "$AUTH_SOURCE" "$CODEX_HOME_DIR/auth.json"
fi

GENERATED_CONFIG="$(mktemp)"
sed \
    -e "s|__MCP_PYTHON__|$MCP_VENV/bin/python|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
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
    -e "s|__RELAY_CONFIG_DIR__|$RELAY_CONFIG_DIR|g" \
    -e "s|__STATE_DIR__|$STATE_DIR|g" \
    codex-jarvis.service.example > "$UNIT_FILE"

# --- Jarvis persona: profanity notice + env (owner + softening list) ------
cat <<'NOTE'

Note: the Jarvis persona ships with profanity and dark humour enabled by
default - a deliberately informal, entertainment-leaning bot. To tone that
down, edit the persona after setup with the  .persona  command in Telegram
(CodexAsk: .xpersona), or edit personas/<instance>.md directly (re-read
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
RELAY_XPERSONA_DIR="$(running_env "$QUEUE_SERVICE_NAME" JARVIS_XPERSONA_DIR)"
if [[ "$RELAY_XPERSONA_DIR" != "$J_PERSONA_DIR" ]]; then
    echo "NOTE: .xpersona edits go through the relay, which writes to its own"
    echo "\$JARVIS_XPERSONA_DIR (currently: ${RELAY_XPERSONA_DIR:-the ClaudeAsk persona directory})."
    echo "Add  JARVIS_XPERSONA_DIR=$J_PERSONA_DIR  to $CLAUDE_JARVIS_DIR/jarvis.env"
    echo "and restart $QUEUE_SERVICE_NAME once no request is in flight."
fi

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

    # The relay's own interpreter and user: the maps must stay readable by it.
    "$RELAY_PYTHON" "$CLAUDE_JARVIS_DIR/cmd_queue.py" --set-owner "$INSTANCE_ID" "$OWNER_TG_ID"
    RELAY_TOKEN="$("$RELAY_PYTHON" "$CLAUDE_JARVIS_DIR/cmd_queue.py" --add-token "$INSTANCE_ID" --worker-env "$WORKER_TOKEN_ENV")"
    echo "Registered owner $OWNER_TG_ID and a new token for instance '$INSTANCE_ID' in $RELAY_CONFIG_DIR"
    echo "(the running relay picks this up by itself; no restart needed)."
    if (( WORKER_ACTIVE )); then
        echo "NOTE: ${SERVICE_NAME}.service is already running and reads $WORKER_TOKEN_ENV"
        echo "only at start. Restart it once no .xask is in flight:"
        echo "  sudo systemctl restart ${SERVICE_NAME}.service"
    fi
else
    echo "No instance ID given; skipping relay token generation."
    echo "Re-run setup.sh once you're ready, or see 'Relay credentials' in the README."
fi

python3 -m py_compile app_server.py codex_ask_watcher.py
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

bash "$ROOT/scripts/setup_userbot_network.sh" codex_ask.py

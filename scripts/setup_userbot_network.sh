#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_NAME="${1:?pass claude_ask.py or codex_ask.py}"
case "$MODULE_NAME" in
    claude_ask.py) MODULE_CLASS=ClaudeAsk ;;
    codex_ask.py) MODULE_CLASS=CodexAsk ;;
    *) echo "Unsupported userbot module: $MODULE_NAME" >&2; exit 1 ;;
esac

fail() { echo "ERROR: $*" >&2; exit 1; }

setup_local_userbot() {
    command -v docker >/dev/null 2>&1 || fail "Docker Engine and the Compose plugin are required: https://docs.docker.com/engine/install/"
    local -a docker_cmd=(docker)
    if ! docker info >/dev/null 2>&1; then
        command -v sudo >/dev/null 2>&1 || fail "Cannot access the Docker daemon; run with Docker access or install sudo"
        sudo docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"
        docker_cmd=(sudo docker)
    fi
    "${docker_cmd[@]}" compose version >/dev/null || fail "Docker Compose plugin is required"

    local project_dir="${TELEGRAM_AI_USERBOT_DIR:-$HOME/.local/share/telegram-ai-userbot}"
    local first_install=0 user_id container_id
    if [[ ! -f "$project_dir/.env" ]]; then
        first_install=1
        user_id="${TELEGRAM_AI_USERBOT_ID:-}"
        if [[ -z "$user_id" && -t 0 ]]; then
            read -rp "Telegram numeric user ID for this userbot: " user_id
        fi
        [[ "$user_id" =~ ^[0-9]+$ ]] || fail "Set a numeric Telegram user ID (or TELEGRAM_AI_USERBOT_ID)"
        mkdir -p "$project_dir/modules"
        chmod 700 "$project_dir"
        printf 'USERBOT_TG_ID=%s\n' "$user_id" > "$project_dir/.env"
        chmod 600 "$project_dir/.env"
    else
        user_id="$(sed -n 's/^USERBOT_TG_ID=\([0-9][0-9]*\)$/\1/p' "$project_dir/.env" | head -n 1)"
        [[ -n "$user_id" ]] || fail "Invalid USERBOT_TG_ID in $project_dir/.env"
        if [[ -n "${TELEGRAM_AI_USERBOT_ID:-}" && "$TELEGRAM_AI_USERBOT_ID" != "$user_id" ]]; then
            fail "Existing userbot belongs to Telegram ID $user_id; refusing to start a second session"
        fi
        mkdir -p "$project_dir/modules"
    fi

    for template in Dockerfile entrypoint.sh compose.yaml; do
        if [[ ! -e "$project_dir/$template" ]]; then
            cp "$REPO_ROOT/deploy/userbot/$template" "$project_dir/$template"
        fi
    done
    cp "$REPO_ROOT/$MODULE_NAME" "$project_dir/modules/$MODULE_CLASS.py"
    if [[ "$MODULE_CLASS" == CodexAsk ]]; then
        cp "$REPO_ROOT/jarvis_ask.py" "$project_dir/modules/JarvisAsk.py"
    fi
    compose() {
        "${docker_cmd[@]}" compose --env-file "$project_dir/.env" \
            --project-directory "$project_dir" -f "$project_dir/compose.yaml" \
            -p telegram-ai-userbot "$@"
    }
    echo "Userbot project: $project_dir (Telegram ID $user_id)"
    if [[ -z "$(compose ps -a -q userbot)" ]]; then
        first_install=1
    fi
    compose up -d --build userbot
    container_id="$(compose ps -q userbot)"
    [[ -n "$container_id" ]] || fail "Userbot container did not start; inspect Docker Compose logs"

    if (( first_install )); then
        echo "Complete Heroku's Telegram sign-in in the container."
        echo "Docker detach keys are Ctrl-P, then Ctrl-Q; this leaves the container running."
        if [[ -t 0 && -t 1 ]]; then
            "${docker_cmd[@]}" attach --sig-proxy=false --detach-keys=ctrl-p,ctrl-q "$container_id"
        else
            echo "From an interactive shell, run: ${docker_cmd[*]} attach --sig-proxy=false --detach-keys=ctrl-p,ctrl-q $container_id"
        fi
    else
        echo "The shared userbot will load newly added modules after a restart; existing installed copies stay unchanged."
        compose restart userbot
    fi
    if [[ "$MODULE_CLASS" == ClaudeAsk ]]; then
        echo "In Telegram: .asknet token <token>, then .asknet local <instance_id>."
    else
        echo "In Telegram: .xasknet token <token>, then .xasknet local <instance_id>."
    fi
    echo "Container logs: ${docker_cmd[*]} compose --env-file $project_dir/.env -f $project_dir/compose.yaml -p telegram-ai-userbot logs -f userbot"
}

setup_remote_tailnet() {
    command -v systemctl >/dev/null 2>&1 || fail "systemd is required for host Tailscale autostart"
    command -v sudo >/dev/null 2>&1 || fail "sudo is required to install and enable host Tailscale"
    if ! command -v tailscale >/dev/null 2>&1; then
        command -v curl >/dev/null 2>&1 || fail "curl is required to download Tailscale's official installer"
        local installer
        installer="$(mktemp)"
        if ! curl -fsSL https://tailscale.com/install.sh -o "$installer"; then
            rm -f "$installer"
            fail "Could not download Tailscale's official installer"
        fi
        if ! sudo sh "$installer"; then
            rm -f "$installer"
            fail "Tailscale installation failed"
        fi
        rm -f "$installer"
    fi
    sudo systemctl enable --now tailscaled.service
    local tailnet_ip
    tailnet_ip="$(sudo tailscale ip -4 2>/dev/null | head -n 1 || true)"
    if [[ -z "$tailnet_ip" ]]; then
        echo "Sign in to the tailnet using the URL printed by Tailscale:"
        sudo tailscale up
        tailnet_ip="$(sudo tailscale ip -4 2>/dev/null | head -n 1 || true)"
    fi
    [[ -n "$tailnet_ip" ]] || fail "Tailscale is not connected; complete login and rerun setup"
    echo "Backend tailnet IP: $tailnet_ip"
    local queue_unit="${TELEGRAM_AI_QUEUE_UNIT:-jarvis-ask-cmd-queue.service}"
    [[ "$queue_unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || fail "Invalid queue service name: $queue_unit"
    local override_dir="/etc/systemd/system/${queue_unit}.d" override_file
    override_file="$(mktemp)"
    printf '[Unit]\nWants=tailscale-online.target\nAfter=tailscale-online.target\n\n[Service]\nEnvironment=JARVIS_RELAY_BIND=127.0.0.1,%s\n' "$tailnet_ip" > "$override_file"
    sudo install -d -m 0755 "$override_dir"
    sudo install -m 0644 "$override_file" "$override_dir/10-tailnet-bind.conf"
    rm -f "$override_file"
    sudo systemctl daemon-reload
    if sudo systemctl is-active --quiet "$queue_unit"; then
        sudo systemctl restart "$queue_unit"
        echo "Updated and restarted $queue_unit with a tailnet-only relay listener."
    else
        echo "Saved $queue_unit override; it will apply when the relay service is installed and started."
    fi
    echo "The remote userbot needs Tailscale in its own container and must join this same tailnet."
    echo "Then run .asknet/.xasknet tailnet <instance_id> http://$tailnet_ip:9092 on the userbot."
}

if [[ ! -t 0 ]]; then
    echo "No interactive terminal; skipping optional userbot/Tailscale setup."
    exit 0
fi

read -rp "Create a local userbot container (1), configure Tailscale for a remote userbot (2), or skip (n)? [1/2/n] " choice
case "${choice,,}" in
    1) setup_local_userbot ;;
    2) setup_remote_tailnet ;;
    n|'') echo "Userbot network setup skipped." ;;
    *) fail "Choose 1, 2, or n" ;;
esac

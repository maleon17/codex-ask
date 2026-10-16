#!/bin/sh
set -eu

case "${USERBOT_TG_ID:-}" in
    ''|*[!0-9]*) echo "USERBOT_TG_ID must be a numeric Telegram user ID" >&2; exit 1 ;;
esac

mkdir -p /data/loaded_modules
for name in JarvisAsk ClaudeAsk CodexAsk; do
    module="/opt/ask-modules/$name.py"
    [ -f "$module" ] || continue
    destination="/data/loaded_modules/${name}_${USERBOT_TG_ID}.py"
    if [ ! -e "$destination" ]; then
        cp "$module" "$destination"
    fi
done

exec "$@"

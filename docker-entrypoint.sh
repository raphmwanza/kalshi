#!/bin/sh
set -e

STRATEGY="${BOT_STRATEGY:-tennis}"
INTERVAL="${BOT_INTERVAL:-30}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

LIVE_FLAG=""
LOOP_FLAG=""

live_on() {
    v=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')
    [ "$v" = "1" ] || [ "$v" = "true" ] || [ "$v" = "yes" ] || [ "$v" = "on" ]
}

# Read-only journal dashboard. No Kalshi key is required. Railway sets PORT.
mode=$(printf '%s' "${BOT_MODE:-trade}" | tr '[:upper:]' '[:lower:]')
if [ "$mode" = "dashboard" ] || [ "${1:-}" = "dashboard" ]; then
    echo "Starting read-only trade dashboard on port ${PORT:-8080}"
    exec python -m src.journal.web
fi

# Drop directory stubs Docker Desktop creates when a file bind-mount is missing.
for f in /app/kalshi_private_key.pem /app/kalshi_private_key; do
    if [ -d "$f" ]; then
        rmdir "$f" 2>/dev/null || true
    fi
done

# PEM body in the environment skips the file entirely.
if [ -z "${KALSHI_PRIVATE_KEY:-}" ]; then
    found=""
    for name in kalshi_private_key.pem kalshi_private_key; do
        if [ -f "/host-project/$name" ]; then
            found="/host-project/$name"
            break
        fi
    done
    if [ -z "$found" ] && [ -n "${KALSHI_PRIVATE_KEY_PATH:-}" ] && [ -f "$KALSHI_PRIVATE_KEY_PATH" ]; then
        found="$KALSHI_PRIVATE_KEY_PATH"
    fi
    if [ -z "$found" ]; then
        for name in kalshi_private_key.pem kalshi_private_key; do
            if [ -f "/app/$name" ]; then
                found="/app/$name"
                break
            fi
        done
    fi
    if [ -z "$found" ]; then
        echo "Kalshi private key not found." >&2
        echo "Place kalshi_private_key.pem or kalshi_private_key in the project root," >&2
        echo "or set KALSHI_PRIVATE_KEY to the PEM body." >&2
        exit 1
    fi
    export KALSHI_PRIVATE_KEY_PATH="$found"
fi

if live_on "${BOT_LIVE:-}"; then
    LIVE_FLAG="--live"
elif live_on "${LIVE_TRADING_ENABLED:-}"; then
    LIVE_FLAG="--live"
fi

if live_on "${BOT_LOOP:-true}"; then
    LOOP_FLAG="--loop"
fi

if [ $# -gt 0 ]; then
    exec python cli.py "$@"
fi

case "$STRATEGY" in
    tennis)
        exec python cli.py run --tennis $LIVE_FLAG $LOOP_FLAG --interval "$INTERVAL" --log-level "$LOG_LEVEL"
        ;;
    safe-compounder)
        exec python cli.py run --safe-compounder $LIVE_FLAG $LOOP_FLAG --interval "$INTERVAL" --log-level "$LOG_LEVEL"
        ;;
    directional)
        exec python cli.py run $LIVE_FLAG --log-level "$LOG_LEVEL"
        ;;
    *)
        echo "Unknown BOT_STRATEGY='$STRATEGY' (use tennis, safe-compounder, or directional)" >&2
        exit 1
        ;;
esac

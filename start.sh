#!/bin/bash
# ===================================================
#  Bot Auto-Start Script (supervisor)
#  VPS / Linux / Replit / Termux
#
#  P5-43: runs the bot in a supervised RESTART LOOP with
#  exponential backoff - a crash no longer leaves the board
#  dead until someone notices. P5-42: a banned bot token is
#  detected via data/BOT_TOKEN_BANNED.marker (written by the
#  bot itself) and backed off hard instead of crash-looping.
#  P5-46: the pyrogram version check no longer reinstalls on
#  every boot (it compares against the REQUIREMENTS pin, not
#  a second hardcoded value that drifted out of sync).
# ===================================================

# Script location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================="
echo "  Telegram Session Bot - Starting..."
echo "========================================="

# Auto-load .env if exists
if [ -f ".env" ]; then
    echo "Loading .env file..."
    set -a
    source .env
    set +a
fi

# P5-46: single source of truth for the pyrogram pin = requirements.txt.
REQUIRED_PYROGRAM_VERSION="$(grep -E '^pyrogram' requirements.txt 2>/dev/null | head -n1 | sed 's/.*==//;s/[[:space:]]//g')"
if [ -z "$REQUIRED_PYROGRAM_VERSION" ]; then
    REQUIRED_PYROGRAM_VERSION="2.0.106"
fi

check_and_install_deps() {
    local need_install=false

    if ! pip show pyrogram > /dev/null 2>&1; then
        need_install=true
    else
        local installed_version
        installed_version=$(pip show pyrogram 2>/dev/null | grep "^Version:" | awk '{print $2}')
        if [ "$installed_version" != "$REQUIRED_PYROGRAM_VERSION" ]; then
            echo "WARNING: Pyrogram $installed_version installed, requirements pin is $REQUIRED_PYROGRAM_VERSION - installing once..."
            need_install=true
        fi
    fi

    if [ "$need_install" = true ]; then
        echo "Installing dependencies..."
        pip install -r requirements.txt --break-system-packages
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to install dependencies!"
            exit 1
        fi
        echo "Dependencies installed successfully!"
    else
        echo "All dependencies OK (Pyrogram $REQUIRED_PYROGRAM_VERSION)"
    fi
}

check_and_install_deps

# P5-43: supervisor loop.
MAX_BACKOFF_SECONDS="${BOT_SUPERVISOR_MAX_BACKOFF:-300}"
backoff=5
consecutive_failures=0

while true; do
    # P5-42: ban marker -> stop crash-looping. The operator must replace the
    # token; restarting every few seconds would only spam Telegram's auth
    # endpoint. Alerting here is impossible from shell, but the marker is
    # checked first thing on every cycle so a restored token resumes.
    if [ -f "data/BOT_TOKEN_BANNED.marker" ]; then
        echo ""
        echo "🚨 SUPERVISOR: data/BOT_TOKEN_BANNED.marker exists - the bot token is"
        echo "   banned/invalid. NOT restarting until the marker is removed."
        echo "   1) Replace BOT_TOKEN in .env with a valid token"
        echo "   2) Delete data/BOT_TOKEN_BANNED.marker"
        echo "   3) Run this script again"
        echo ""
        sleep 60
        continue
    fi

    echo "Starting bot... (consecutive failures: $consecutive_failures)"
    python bot/main.py
    exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        echo "Bot exited cleanly (code 0)."
        # A clean exit (e.g. graceful SIGTERM) -> reset the backoff but do
        # NOT loop: someone intentionally stopped the bot.
        echo "Supervisor exiting (clean exit). Use a service manager to auto-restart on demand."
        break
    fi

    if [ "$exit_code" -eq 2 ]; then
        echo "🚨 Bot exited with code 2 (banned/invalid token). Backing off; will re-check the marker."
        consecutive_failures=$((consecutive_failures + 1))
    else
        consecutive_failures=$((consecutive_failures + 1))
    fi

    if [ "$consecutive_failures" -ge 1 ]; then
        echo "Bot crashed (exit code $exit_code). Restarting in ${backoff}s..."
        sleep "$backoff"
        # Exponential backoff, capped.
        backoff=$((backoff * 2))
        if [ "$backoff" -gt "$MAX_BACKOFF_SECONDS" ]; then
            backoff=$MAX_BACKOFF_SECONDS
        fi
    fi
done

#!/usr/bin/env bash
# setup_macos.sh — one-command macOS setup for the AI News Agent
#
# The agent runs on your Claude subscription via the Claude Code CLI —
# NO Anthropic API key or credits needed.
#
# What this does:
#   1. Checks Python 3 is available
#   2. Installs Python deps (pyyaml)
#   3. Finds the Claude Code CLI and checks you're logged in
#   4. Optionally stores an EMAIL_PASSWORD in the Keychain
#   5. Reads schedule (hour/minute) from config.yaml
#   6. Writes a launchd plist to ~/Library/LaunchAgents/
#   7. Loads (or reloads) the agent so it runs daily
#   8. Runs a --dry-run to verify everything works
#
# Re-run any time you change the schedule in config.yaml.
# Usage: bash setup_macos.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_LABEL="com.ainews.daily"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PLIST_TEMPLATE="${SCRIPT_DIR}/com.ainews.daily.plist"
CONFIG="${SCRIPT_DIR}/config.yaml"
LOG_DIR="${SCRIPT_DIR}/logs"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

echo ""
echo "══════════════════════════════════════════════"
echo "  AI News Agent — macOS Setup (subscription)"
echo "══════════════════════════════════════════════"
echo ""

# ── 1. Python 3 ───────────────────────────────────────────────────────────────
PYTHON=$(command -v python3) || die "python3 not found. Install from python.org or via Homebrew."
ok "Python 3 found: $PYTHON ($(python3 --version 2>&1))"

# ── 2. Dependencies ───────────────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies..."
pip3 install --quiet --upgrade pyyaml
ok "pyyaml installed"

# ── 3. Claude Code CLI ────────────────────────────────────────────────────────
echo ""
CLAUDE_BIN=""
if command -v claude >/dev/null 2>&1; then
    CLAUDE_BIN="$(command -v claude)"
elif [[ -x "$HOME/.local/bin/claude" ]]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
else
    # Bundled with the Claude desktop app — pick the newest version
    CLAUDE_BIN=$(ls -1d "$HOME/Library/Application Support/Claude/claude-code/"*"/claude.app/Contents/MacOS/claude" 2>/dev/null | sort -V | tail -1 || true)
fi
[[ -n "$CLAUDE_BIN" && -x "$CLAUDE_BIN" ]] || die "Claude Code CLI not found. Install the Claude desktop app or the CLI (https://claude.com/claude-code)."
ok "Claude Code CLI: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>/dev/null | head -1))"

# Login check — uses your subscription, costs nothing extra
echo "  Checking Claude login (takes a few seconds)..."
LOGIN_OK="false"
if AUTH_TEST=$("$CLAUDE_BIN" -p "Reply with exactly: OK" --model claude-haiku-4-5 2>&1); then
    if [[ "$AUTH_TEST" == *"OK"* ]]; then
        LOGIN_OK="true"
        ok "Claude CLI is logged in — agent will run on your subscription"
    fi
fi
if [[ "$LOGIN_OK" != "true" ]]; then
    warn "Claude CLI is NOT logged in yet."
    echo ""
    echo "  One-time fix — run this in Terminal:"
    echo ""
    echo "      \"$CLAUDE_BIN\""
    echo ""
    echo "  then type  /login  and sign in with your Claude account"
    echo "  (the same one you use in the Claude app — no API key needed)."
    echo "  Setup will continue, but daily runs will fail until you log in."
    echo ""
fi

# ── 4. Email password (optional) ──────────────────────────────────────────────
EMAIL_PASSWORD=""
EMAIL_KEYCHAIN_SERVICE="ainews-email-password"

# Only ask if email is enabled in config
EMAIL_ENABLED=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('${CONFIG}'))
print('true' if cfg.get('delivery',{}).get('email',{}).get('enabled') else 'false')
")

if [[ "$EMAIL_ENABLED" == "true" ]]; then
    echo ""
    echo "Email delivery is enabled in config.yaml."
    EXISTING_EMAIL_PW=$(security find-generic-password -a "$USER" -s "$EMAIL_KEYCHAIN_SERVICE" -w 2>/dev/null || true)
    if [[ -z "$EXISTING_EMAIL_PW" ]]; then
        read -rsp "  Paste your email/app password (input hidden, or Enter to skip): " EMAIL_PASSWORD
        echo ""
        if [[ -n "$EMAIL_PASSWORD" ]]; then
            security add-generic-password -a "$USER" -s "$EMAIL_KEYCHAIN_SERVICE" -w "$EMAIL_PASSWORD"
            ok "Email password saved to Keychain"
        else
            warn "Email password skipped. Set the EMAIL_PASSWORD env var manually if needed."
        fi
    else
        ok "Email password already in Keychain"
        EMAIL_PASSWORD="$EXISTING_EMAIL_PW"
    fi
fi

# ── 5. Read schedule from config.yaml ─────────────────────────────────────────
HOUR=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('${CONFIG}'))
print(cfg.get('schedule', {}).get('hour', 8))
")
MINUTE=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('${CONFIG}'))
print(cfg.get('schedule', {}).get('minute', 0))
")
ok "Schedule: daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")"

# ── 6. Write plist ────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

sed \
  -e "s|__PYTHON3__|${PYTHON}|g" \
  -e "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" \
  -e "s|__HOUR__|${HOUR}|g" \
  -e "s|__MINUTE__|${MINUTE}|g" \
  "$PLIST_TEMPLATE" > "$PLIST_DEST"

# Inject email password block if present (replace the comment placeholder)
if [[ -n "$EMAIL_PASSWORD" ]]; then
    python3 - <<PYEOF
import re, pathlib
path = pathlib.Path('${PLIST_DEST}')
text = path.read_text()
text = re.sub(
    r'<!--\s*Uncomment.*?-->\n',
    '    <key>EMAIL_PASSWORD</key>\n    <string>${EMAIL_PASSWORD}</string>\n',
    text, flags=re.DOTALL
)
path.write_text(text)
PYEOF
fi

ok "Plist written to $PLIST_DEST"

# ── 7. Load / reload launchd job ──────────────────────────────────────────────
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"
ok "launchd job loaded — agent will run daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")"

# ── 8. Dry-run verification ───────────────────────────────────────────────────
echo ""
echo "Running --dry-run to verify config..."
python3 "${SCRIPT_DIR}/ai_news_agent.py" --dry-run

echo ""
echo "══════════════════════════════════════════════"
ok "Setup complete!"
echo ""
echo "  • Agent runs daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") on your Claude subscription (no API cost)"
echo "  • Logs: ${LOG_DIR}/ai_news.log"
echo "  • Run now: python3 \"${SCRIPT_DIR}/ai_news_agent.py\""
echo "  • To change schedule: edit config.yaml → re-run bash setup_macos.sh"
echo "  • To uninstall: launchctl unload $PLIST_DEST && rm $PLIST_DEST"
if [[ "$LOGIN_OK" != "true" ]]; then
    echo ""
    warn "Remember: log in the Claude CLI first (see step 3 above) or daily runs will fail."
fi
echo "══════════════════════════════════════════════"
echo ""

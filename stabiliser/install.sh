#!/usr/bin/env bash
# Generates the LaunchAgent plist, installs it, and starts the watcher.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.$(whoami).stabiliser"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Movies/GoPro-Utils/Stabiliser/Logs"

# ---------------------------------------------------------------------------
# Resolve Python 3.13 — prefer the Homebrew install over system Python
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in \
    "/usr/local/opt/python@3.13/libexec/bin/python3" \
    "/opt/homebrew/opt/python@3.13/libexec/bin/python3" \
    "$(command -v python3 2>/dev/null || true)"
do
    if [[ -x "$candidate" ]] && "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3,13) else 1)" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.13+ not found. Install it with: brew install python@3.13"
    exit 1
fi

echo "Using Python : $PYTHON"
echo "Script       : $SCRIPT_DIR/stabilise_watch.py"
echo "Label        : $LABEL"
echo "Plist        : $PLIST"

# ---------------------------------------------------------------------------
# Write the plist
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/stabilise_watch.py</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd.error.log</string>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
</dict>
</plist>
EOF

# ---------------------------------------------------------------------------
# Load (or reload) the agent
# ---------------------------------------------------------------------------
if launchctl list | grep -q "$LABEL"; then
    echo "Reloading existing agent …"
    launchctl unload "$PLIST"
fi

launchctl load "$PLIST"

# Verify
sleep 1
if launchctl list | grep -q "$LABEL"; then
    STATUS=$(launchctl list | grep "$LABEL")
    echo "Started: $STATUS"
else
    echo "WARNING: agent did not appear in launchctl list — check $LOG_DIR/launchd.error.log"
    exit 1
fi

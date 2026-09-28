#!/usr/bin/env bash
# Run the monitor as a macOS background service (launchd).
#   ./service.sh install    start now + auto-start at login + auto-restart on crash
#   ./service.sh uninstall  stop and remove
#   ./service.sh status     is it running?
#   ./service.sh logs       follow the log
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.pricegitch.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

case "${1:-}" in
  install)
    [ -f config.json ] || { echo "Run the setup first: .venv/bin/python run.py --setup"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" logs
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <!-- caffeinate -i keeps the Mac from idle-sleeping while the monitor runs -->
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$DIR/.venv/bin/python</string><string>-W</string><string>ignore</string>
    <string>$DIR/run.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DIR/logs/service.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/service.err.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "✅ Installed. Running in the background and will start automatically at login."
    echo "   Logs: ./service.sh logs     Stop: ./service.sh uninstall"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Stopped and removed."
    ;;
  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =" | head -2
    else
      echo "Not installed."
    fi
    ;;
  logs)
    tail -n 50 -f logs/monitor.log
    ;;
  *)
    echo "Usage: ./service.sh install|uninstall|status|logs"; exit 1 ;;
esac

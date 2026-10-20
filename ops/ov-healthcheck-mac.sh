#!/bin/bash
# Daily OpenViking health check from the Mac: runs ops/healthcheck.py on devbox over ssh,
# logs to ~/Library/Logs/ov-healthcheck.log and posts a macOS notification on problems.
#   ov-healthcheck-mac.sh             run once
#   ov-healthcheck-mac.sh --install   [HH:MM, default 09:30] install LaunchAgent com.openviking.healthcheck
#   ov-healthcheck-mac.sh --uninstall
set -uo pipefail
LABEL=com.openviking.healthcheck; PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG=$HOME/Library/Logs/ov-healthcheck.log; SELF=$HOME/.local/bin/ov-healthcheck
case "${1:-}" in
  --install)
    t=${2:-09:30}; h=$((10#${t%:*})); m=$((10#${t#*:}))
    mkdir -p "$(dirname "$SELF")" && cp "$0" "$SELF" && chmod +x "$SELF"
    cat > "$PLIST" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$SELF</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>$m</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string><key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
P
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "installed $LABEL at $t"; exit;;
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; rm -f "$PLIST" "$SELF"; echo "uninstalled"; exit;;
esac
out=$(ssh -o ConnectTimeout=30 -o BatchMode=yes "${OV_HEALTH_HOST:-devbox}" 'python3.13 ~/.openviking/local_patches/ov-fork/ops/healthcheck.py' 2>&1); rc=$?
printf '%s rc=%s\n%s\n' "$(date '+%F %T')" "$rc" "$out" >> "$LOG"
if [ "$rc" -ne 0 ]; then
  msg=$(printf '%s' "$out" | head -1 | cut -c1-200 | tr '"' "'")
  osascript -e "display notification \"$msg\" with title \"OpenViking 巡检\" subtitle \"详情: ~/Library/Logs/ov-healthcheck.log\""
fi
exit $rc

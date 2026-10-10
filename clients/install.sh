#!/bin/bash
# Install the Mac-side OpenViking client additions from this checkout (idempotent).
#   clients/install.sh            link/copy everything, then check the host wiring
# artifact-capture and the replay skill are symlinked (edit them here); the pi
# extension is copied, because pi resolves its relative import from the install path.
set -euo pipefail
C=$(cd "$(dirname "$0")" && pwd); AI=$HOME/.openviking/agent-integrations
link() {  # link <target> <path>: replace a real dir/file with a symlink, keeping a backup
  if [ -e "$2" ] && [ ! -L "$2" ]; then mv "$2" "$2.pre-link-$(date +%Y%m%d%H%M%S)"; fi
  ln -sfn "$1" "$2"; echo "link $2 -> $1"
}
mkdir -p "$AI/skills"
link "$C/artifact-capture" "$AI/artifact-capture"
link "$C/skills/ov-session-replay" "$AI/skills/ov-session-replay"
for d in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.pi/agent/skills"; do
  [ -d "$d" ] && link "$AI/skills/ov-session-replay" "$d/ov-session-replay"
done
install -m 644 "$C/pi/openviking-artifacts.ts" "$HOME/.pi/agent/extensions/openviking-artifacts.ts"
echo "copy ~/.pi/agent/extensions/openviking-artifacts.ts"
# host wiring the OpenViking installer does not manage (it never removes these either)
for f in "$HOME/.claude/settings.json" "$HOME/.codex/hooks.json"; do
  grep -q "artifact-capture/hook.mjs" "$f" 2>/dev/null && echo "ok   $f has the artifact-capture Stop/SubagentStop hooks" \
    || echo "MISSING artifact-capture hooks in $f: add Stop and SubagentStop -> node $AI/artifact-capture/hook.mjs <claude-code|codex>"
done

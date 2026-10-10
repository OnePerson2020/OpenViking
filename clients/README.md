# Mac-side client additions

What the OpenViking memory plugins do not do, kept outside the plugin directories
(the official installer replaces those on every upgrade) and versioned here.

- `artifact-capture/`: images and files an agent produced or attached, for Claude Code,
  Codex and pi → local blob + manifest (`~/.openviking/artifact-capture/`, durable before
  any network I/O) → `viking://resources/agent-artifacts/<harness>/<ovSessionId>/` + a link
  message in the same OV session. CC/Codex run `hook.mjs` on Stop/SubagentStop
  (`~/.claude/settings.json`, `~/.codex/hooks.json`); `backfill-codex.mjs` replays missed
  Codex threads. Tests: `node --test artifact-capture/core.test.mjs`.
- `pi/openviking-artifacts.ts`: the same for pi, as its own extension next to the official
  `openviking` one (message_end: stage locally; turn_end/session_start: upload).
- `skills/ov-session-replay/`: replay past sessions L0→L3 (no upstream equivalent as of
  v0.5.0), linked into the CC/Codex/pi skill dirs.

Upstream v0.5.0 still uploads no images or produced files in any plugin, so all three stay.

Install / after an OpenViking plugin upgrade: `clients/install.sh` (also checks the hooks).
Plugins themselves: the official installer, e.g.
`curl -fsSL https://openviking.ai/install | env -u FORCE_COLOR bash -s -- --harness claude,codex,pi,trae-cn --no-statusline --yes`
(`FORCE_COLOR` breaks its Node version check; `--no-statusline` keeps claude-hud).
pi plugin settings live in `~/.openviking/ovcli.conf` → `plugin.pi` since v0.5.0.

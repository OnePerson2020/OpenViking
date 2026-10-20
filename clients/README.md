# Mac-side client additions

What the OpenViking memory plugins do not do, kept outside the plugin directories
(the official installer replaces those on every upgrade) and versioned here.

- `artifact-capture/`: images and files an agent produced or attached, for Claude Code,
  Codex and pi → local blob + manifest (`~/.openviking/artifact-capture/`, durable before
  any network I/O) → `viking://resources/agent-artifacts/<harness>/<ovSessionId>/` + a link
  message in the same OV session. CC/Codex run `hook.mjs` on Stop/SubagentStop
  (`~/.claude/settings.json`, `~/.codex/hooks.json`); `backfill-codex.mjs` replays missed
  Codex threads. Tests: `node --test artifact-capture/*.test.mjs`.
  Codex completed FileChange events capture edits made through exec wrappers too; unfinished
  JSONL tails remain behind the cursor until the next hook can read them completely.
- `pi/openviking-artifacts.ts`: the same for pi, as its own extension next to the official
  `openviking` one (message_end: stage locally; turn_end/session_start: upload).
- `recall-gate/`: Claude Code UserPromptSubmit gate. One bjev call (LocalBridge `/v1/systemone`,
  1 s timeout) scores whether the prompt needs memory and whether it can be read without the
  earlier turns. Below 0.15 need: no recall. Context-dependent with need >= 0.4: the plugin's
  `auto-recall.mjs` with its query-expansion planner, which reads the session. Otherwise, or on
  any bjev failure: the same script with `OPENVIKING_RECALL_QUERY_EXPANSION=off`, recalling on
  the prompt as written. Needs `plugin.claude_code.autoRecall: false` in ovcli.conf so the
  plugin's own hook stands down; the gate re-enables it per call with `OPENVIKING_AUTO_RECALL=1`.
  Prompts the plugin's `recallQueryFilters` drop skip before bjev; ovcli.conf drops teammate
  messages and task notifications with `d/^<(agent-message|task-notification)\b/`. Decisions:
  `~/.openviking/logs/recall-gate.log`. Test: `node --test recall-gate/*.test.mjs`; re-tune with
  `node recall-gate/eval.mjs`. Plugin surfaces it relies on (install.sh checks them): `scripts/
  auto-recall.mjs`, `scripts/config.mjs` `loadConfig`, `scripts/shared/input-filters.mjs`, and the
  `OPENVIKING_AUTO_RECALL` / `OPENVIKING_RECALL_QUERY_EXPANSION` env overrides.
- `skills/ov-session-replay/`: replay past sessions L0→L3 (no upstream equivalent as of
  v0.5.0), linked into the CC/Codex/pi skill dirs.

Upstream v0.5.0 still uploads no images or produced files in any plugin, so all three stay.

Install / after an OpenViking plugin upgrade: `clients/install.sh` (also checks the hooks).
Plugins themselves: the official installer, e.g.
`curl -fsSL https://openviking.ai/install | env -u FORCE_COLOR bash -s -- --harness claude,codex,pi,trae-cn --no-statusline --yes`
(`FORCE_COLOR` breaks its Node version check; `--no-statusline` keeps claude-hud).
pi plugin settings live in `~/.openviking/ovcli.conf` → `plugin.pi` since v0.5.0.

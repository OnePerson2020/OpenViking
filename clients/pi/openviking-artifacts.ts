// Durable OpenViking capture of pi's images and produced files, shared with the
// Claude Code / Codex Stop hooks (~/.openviking/agent-integrations/artifact-capture).
// Kept outside ~/.pi/agent/extensions/openviking because the OpenViking installer
// replaces that directory on every upgrade. Images and files go to
// viking://resources/agent-artifacts/pi/<ovSessionId>/ plus a link message in the
// same OV session (pi-<sessionId>, as the official extension names it).
// ponytail: ignores the official extension's bypassSessionPatterns (none set); a
// bypassed session would still get artifact links.
import {
  createClient, createStore, loadConnection, scanPi, stage,
} from "../../../.openviking/agent-integrations/artifact-capture/core.mjs";

export default function (pi: any) {
  let store: any, client: any;
  try {
    const conn = loadConnection();
    store = createStore({ harness: "pi", conn });
    client = createClient(conn);
  } catch { return; } // OpenViking not configured
  const scanned = new Map<string, number>(); // pi session -> branch entries already scanned
  const log = (m: string) => { if (process.env.OV_DEBUG_LOG) console.error(`[openviking-artifacts] ${m}`); };

  // Local blob + manifest only (no network), so an interrupted turn is retried later.
  function capture(ctx: any) {
    const sid = ctx.sessionManager.getSessionId();
    const branch = ctx.sessionManager.getBranch();
    if (!sid || !Array.isArray(branch)) return;
    const from = Math.min(scanned.get(sid) ?? 0, branch.length); // a fork can shorten the branch
    scanned.set(sid, branch.length);
    stage(store, `pi-${sid}`, scanPi(branch.slice(from), { cwd: process.cwd() }), log);
  }
  const drain = () => void store.drain(client, log).catch(() => {});

  pi.on("session_start", async () => drain()); // retry what earlier sessions left pending
  pi.on("message_end", async (_e: any, ctx: any) => { try { capture(ctx); } catch (e: any) { log(e?.message); } });
  pi.on("turn_end", async (_e: any, ctx: any) => { try { capture(ctx); } catch (e: any) { log(e?.message); } drain(); });
}

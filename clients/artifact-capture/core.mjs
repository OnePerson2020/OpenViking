// Durable artifact capture for Claude Code / Codex / pi sessions.
//
// The memory plugins upload conversation text and tool calls, but drop image
// blocks and never upload the files an agent produced. This module fills that
// gap without touching the plugins (which are replaced on every upgrade):
//
//   transcript -> artifacts (image blocks, files written by the agent, local
//   artifact paths mentioned in the turn) -> local blob + manifest (durable
//   before any network I/O) -> OV resource under
//   viking://resources/agent-artifacts/<harness>/<ovSessionId>/ -> a link
//   message in the same OV session so replay and extraction can find it.
//
// Each manifest moves pending -> submitted -> linked and is retried on every
// later hook run until linked. Delivery is at-least-once: a crash between the
// server accepting a link and the manifest save can duplicate a link message.
import { createHash, randomUUID } from "node:crypto";
import {
  closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync,
  renameSync, statSync, unlinkSync, writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, extname, isAbsolute, join, resolve } from "node:path";

export const MAX_FILE_BYTES = 50 * 1024 * 1024;
// Server per-file limit of POST /api/v1/content/batch-write (_BATCH_MAX_FILE_BYTES); a larger
// original can never be stored, so it is skipped instead of retried on every hook run.
export const ORIGINAL_MAX_BYTES = 8 * 1024 * 1024;
export const ARTIFACT_ROOT = "viking://resources/agent-artifacts";
const IMAGE_MIME_EXT = new Map([
  ["image/png", "png"], ["image/jpeg", "jpg"], ["image/jpg", "jpg"], ["image/gif", "gif"],
  ["image/webp", "webp"], ["image/bmp", "bmp"], ["image/tiff", "tiff"], ["image/svg+xml", "svg"],
]);
// Binary/rich artifacts worth uploading whenever their local path is mentioned.
// Plain source files are captured only when the agent itself wrote them.
export const MENTIONED_EXTS = [
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg",
  "pdf", "docx", "xlsx", "pptx", "zip", "html", "htm", "ipynb",
];
const SKIP_DIRS = /(?:^|\/)(?:node_modules|\.git|__pycache__|\.venv|\.cache)(?:\/|$)/;

export const sha256 = (value) => createHash("sha256").update(value).digest("hex");

// Text artifacts up to this size travel verbatim as a tool output in the link
// message: the server externalizes large outputs into its tool-result store
// (hash-checked, byte-exact replay) without an extra semantic/vector pass.
export const MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024;

export function isInlineText(bytes) {
  if (bytes.length > MAX_INLINE_TEXT_BYTES || bytes.includes(0)) return false;
  try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); return true; } catch { return false; }
}

function privateDir(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
}

function atomicWrite(path, value) {
  const tmp = `${path}.${randomUUID()}.tmp`;
  try {
    const fd = openSync(tmp, "wx", 0o600);
    try { writeFileSync(fd, value); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(tmp, path);
  } finally {
    try { unlinkSync(tmp); } catch { /* renamed */ }
  }
}

export function sanitizeName(filePath) {
  const base = basename(String(filePath || ""));
  const ext = extname(base);
  const stem = (ext ? base.slice(0, -ext.length) : base)
    .normalize("NFKD").replace(/[^\w.-]+/g, "-").replace(/-+/g, "-").replace(/^-+|-+$/g, "")
    .slice(0, 80) || "artifact";
  return `${stem}${ext.replace(/[^.\w]+/g, "").toLowerCase()}`;
}

// ---------------------------------------------------------------- connection

export function loadConnection(env = process.env) {
  let file = {};
  try {
    file = JSON.parse(readFileSync(join(homedir(), ".openviking", "ovcli.conf"), "utf8"));
  } catch { /* env-only setups */ }
  const url = String(env.OPENVIKING_URL || env.OPENVIKING_BASE_URL || file.url || "").replace(/\/+$/, "");
  const apiKey = String(env.OPENVIKING_BEARER_TOKEN || env.OPENVIKING_API_KEY || file.api_key || "");
  if (!url) throw new Error("OpenViking URL is not configured (~/.openviking/ovcli.conf)");
  return { url, apiKey };
}

export function createClient(conn, { timeoutMs = 120_000 } = {}) {
  const headers = conn.apiKey ? { Authorization: `Bearer ${conn.apiKey}` } : {};
  async function call(path, init = {}) {
    const res = await fetch(`${conn.url}${path}`, {
      ...init,
      headers: { ...headers, ...(init.headers || {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text.slice(0, 500) }; }
    if (!res.ok || (body && body.status === "error")) {
      const detail = body?.error?.message || body?.detail || body?.raw || res.statusText;
      throw new Error(`OV ${init.method || "GET"} ${path} -> ${res.status}: ${String(detail).slice(0, 300)}`);
    }
    return body?.result ?? body;
  }
  return {
    async uploadResource(blobPath, { to, reason }) {
      const form = new FormData();
      form.append("file", new Blob([readFileSync(blobPath)]), basename(to));
      const up = await call("/api/v1/resources/temp_upload", { method: "POST", body: form });
      const tempId = up?.temp_file_id;
      if (!tempId) throw new Error("temp_upload returned no temp_file_id");
      const added = await call("/api/v1/resources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ temp_file_id: tempId, to, reason, wait: false }),
      });
      const uri = added?.root_uri || added?.uri;
      if (typeof uri !== "string" || !uri.startsWith("viking://resources/")) {
        throw new Error("resource submission returned no viking://resources/ URI");
      }
      return uri;
    },
    /**
     * Byte-exact original (resource parsing keeps only Markdown for HTML/PDF/
     * Office). Stored beside, not inside, the parsed resource so it does not
     * wait for that resource's parse lock.
     */
    async writeOriginal(uri, blobPath) {
      await call("/api/v1/content/batch-write", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_uri: ARTIFACT_ROOT,
          operations: [{ uri, content_base64: readFileSync(blobPath).toString("base64"), mode: "upsert" }],
          wait: false,
        }),
      });
      return uri;
    },
    async addMessage(ovSessionId, role, content, parts) {
      await call(`/api/v1/sessions/${encodeURIComponent(ovSessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parts ? { role, parts } : { role, content }),
      });
    },
  };
}

// ---------------------------------------------------------------- decoding

export function decodeBase64Image(mimeType, data) {
  const mime = String(mimeType || "").toLowerCase().trim();
  const ext = IMAGE_MIME_EXT.get(mime);
  if (!ext || typeof data !== "string") throw new Error(`unsupported image MIME ${mime || "(none)"}`);
  const clean = data.replace(/\s/g, "");
  if (!clean || /[^A-Za-z0-9+/=]/.test(clean)) throw new Error("invalid image base64");
  const bytes = Buffer.from(clean, "base64");
  if (!bytes.length || bytes.toString("base64").replace(/=+$/, "") !== clean.replace(/=+$/, "")) {
    throw new Error("invalid image base64");
  }
  return { bytes, ext, mime };
}

export function decodeDataUrl(url) {
  const m = /^data:([^;,]+)(?:;[^;,]*)*;base64,(.*)$/s.exec(String(url || ""));
  if (!m) throw new Error("not a base64 data URL");
  return decodeBase64Image(m[1], m[2]);
}

// ---------------------------------------------------------------- mentions

const MD_LINK_RE = /!?\[[^\]]*\]\(\s*([^)\s]+)/g;
const BARE_PATH_RE = /(?:^|[\s"'`(<=])((?:\/|~\/)[^\s"'`)>]+)/g;

export function extractMentionedPaths(text, exts = MENTIONED_EXTS) {
  const value = String(text || "");
  if (!value) return [];
  const extSet = new Set(exts);
  const out = [];
  const seen = new Set();
  const consider = (raw) => {
    let p = String(raw || "").trim();
    if (p.startsWith("file://")) p = p.slice(7);
    p = p.replace(/[)>\].,;:!?'"`，。；：！？、]+$/u, "");
    if (!(p.startsWith("/") || p.startsWith("~/"))) return;
    let best = -1;
    for (let i = 0; i < p.length; i++) {
      if (p[i] !== ".") continue;
      let j = i + 1;
      while (j < p.length && /[A-Za-z0-9]/.test(p[j])) j++;
      if (extSet.has(p.slice(i + 1, j).toLowerCase())) best = j;
    }
    if (best < 0) return;
    p = p.slice(0, best);
    if (p.startsWith("~/")) p = join(homedir(), p.slice(2));
    if (!seen.has(p)) { seen.add(p); out.push(p); }
  };
  let m;
  while ((m = MD_LINK_RE.exec(value)) !== null) consider(m[1]);
  MD_LINK_RE.lastIndex = 0;
  while ((m = BARE_PATH_RE.exec(value)) !== null) consider(m[1]);
  BARE_PATH_RE.lastIndex = 0;
  return out;
}

export function resolveAgentPath(p, cwd) {
  if (typeof p !== "string" || !p.trim()) return null;
  let value = p.trim();
  if (value.startsWith("~/")) value = join(homedir(), value.slice(2));
  if (!isAbsolute(value)) {
    if (!cwd) return null;
    value = resolve(cwd, value);
  }
  return value;
}

// ---------------------------------------------------------------- store

export function createStore({ harness, conn, rootDir = join(homedir(), ".openviking", "artifact-capture") }) {
  // Pending uploads never cross account/endpoint boundaries.
  const scope = sha256(JSON.stringify([conn.url, conn.apiKey])).slice(0, 32);
  const dir = join(rootDir, scope);
  const blobsDir = join(dir, "blobs");
  const recordsDir = join(dir, "records");
  const cursorsDir = join(dir, "cursors");
  for (const path of [rootDir, dir, blobsDir, recordsDir, cursorsDir]) privateDir(path);

  const blobPath = (sha, ext) => join(blobsDir, `${sha}.${ext || "bin"}`);
  const recordPath = (key) => join(recordsDir, `${key}.json`);

  function putBlob(bytes, ext) {
    const sha = sha256(bytes);
    const path = blobPath(sha, ext);
    if (!existsSync(path)) atomicWrite(path, bytes);
    return sha;
  }

  /** Idempotent: one record per (session, artifact identity, content hash). */
  function addRecord(record) {
    const key = sha256(JSON.stringify([record.ovSessionId, record.kind, record.identity, record.sha256]));
    if (existsSync(recordPath(key))) return false;
    atomicWrite(recordPath(key), JSON.stringify({ version: 1, harness, status: "pending", ...record }));
    return true;
  }

  function addImage({ ovSessionId, role, mime, bytes, ext, identity, origin }) {
    const sha = putBlob(bytes, ext);
    return addRecord({ ovSessionId, kind: "image", role, mime, ext, sha256: sha, identity, origin,
      name: `${origin?.label || "image"}.${ext}` });
  }

  function addFile({ ovSessionId, role, path, origin }) {
    if (SKIP_DIRS.test(path)) return false;
    let st;
    try { st = statSync(path); } catch { return false; }
    if (!st.isFile() || st.size <= 0 || st.size > MAX_FILE_BYTES) return false;
    const bytes = readFileSync(path);
    const ext = extname(path).slice(1).toLowerCase() || "bin";
    const sha = putBlob(bytes, ext);
    return addRecord({ ovSessionId, kind: "file", role, ext, sha256: sha, identity: path, origin,
      path, size: st.size, mtime: st.mtime.toISOString(), name: sanitizeName(path) });
  }

  function cursor(transcriptPath) {
    const path = join(cursorsDir, `${sha256(transcriptPath).slice(0, 32)}.json`);
    let value = 0;
    try { value = JSON.parse(readFileSync(path, "utf8")).lines || 0; } catch { /* fresh */ }
    return { value, save: (lines) => atomicWrite(path, JSON.stringify({ transcriptPath, lines })) };
  }

  function records() {
    const out = [];
    for (const name of readdirSync(recordsDir)) {
      if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
      try { out.push([name.slice(0, -5), JSON.parse(readFileSync(join(recordsDir, name), "utf8"))]); } catch { /* skip */ }
    }
    return out;
  }

  /** Lock so overlapping hook workers do not upload the same record twice. */
  function withLock(fn) {
    const lockPath = join(dir, "drain.lock");
    try {
      const st = statSync(lockPath);
      const owner = Number(readFileSync(lockPath, "utf8")) || 0;
      let alive = false;
      try { if (owner) { process.kill(owner, 0); alive = true; } } catch { /* owner gone */ }
      if (alive && Date.now() - st.mtimeMs < 30 * 60_000) return Promise.resolve({ skipped: true });
      unlinkSync(lockPath);
    } catch { /* no lock */ }
    let fd;
    try { fd = openSync(lockPath, "wx", 0o600); } catch { return Promise.resolve({ skipped: true }); }
    writeFileSync(fd, String(process.pid));
    closeSync(fd);
    return Promise.resolve().then(fn).finally(() => { try { unlinkSync(lockPath); } catch { /* gone */ } });
  }

  async function drain(client, log = () => {}) {
    return withLock(async () => {
      let linked = 0;
      let failed = 0;
      for (const [key, record] of records()) {
        if (record.status === "linked") continue;
        try {
          const blob = blobPath(record.sha256, record.ext);
          if (sha256(readFileSync(blob)) !== record.sha256) throw new Error("stored blob hash mismatch");
          // the record's own harness: any hook drains the shared store (a Codex hook drained pi records)
          const sessionDir = `${ARTIFACT_ROOT}/${record.harness || harness}/${record.ovSessionId}`;
          const fileName = `${record.sha256.slice(0, 12)}-${record.name}`;
          if (record.status === "pending") {
            const to = `${sessionDir}/${fileName}`;
            // No `reason`: the server turns every reason into a commit on one shared
            // session that serializes behind itself and blocks AddResource workers;
            // the session link below already records where the artifact came from.
            record.resourceUri = await client.uploadResource(blob, { to });
            record.status = "submitted";
            atomicWrite(recordPath(key), JSON.stringify(record));
          }
          const bytes = readFileSync(blob);
          const inlineText = record.kind === "file" && isInlineText(bytes);
          if (record.status === "submitted") {
            if (record.kind === "image") {
              record.originalUri = record.resourceUri; // the parsed image resource keeps the original file
            } else if (!inlineText && bytes.length > ORIGINAL_MAX_BYTES) {
              record.originalSkipped = `original over the ${ORIGINAL_MAX_BYTES / 1048576} MiB batch-write limit`;
            } else if (!inlineText) {
              // Binary documents: parsing keeps only Markdown, so store the bytes.
              // A busy/missing root stays "submitted" and is retried later.
              record.originalUri = await client.writeOriginal(`${sessionDir}/.originals/${fileName}`, blob);
            }
            record.status = "raw_saved";
            atomicWrite(recordPath(key), JSON.stringify(record));
          }
          const meta = {
            kind: record.kind, resource_uri: record.resourceUri, original_uri: record.originalUri,
            ...(record.originalSkipped ? { original_skipped: record.originalSkipped } : {}),
            sha256: record.sha256,
            ...(record.path ? { path: record.path, bytes: record.size, mtime: record.mtime } : {}),
            ...(record.mime ? { mime_type: record.mime } : {}),
            ...(record.origin ? { origin: record.origin } : {}),
          };
          const title = record.kind === "image" ? "[Image attachment]" : "[Artifact snapshot]";
          const header = `${title} ${record.path || record.name}\n${JSON.stringify(meta)}`;
          const parts = inlineText ? [
            { type: "text", text: header },
            { type: "tool", tool_id: `artifact-${record.sha256.slice(0, 16)}`, tool_name: "artifact_snapshot",
              tool_input: { path: record.path, sha256: record.sha256, resource_uri: record.resourceUri },
              tool_output: bytes.toString("utf8"), tool_status: "completed" },
          ] : null;
          await client.addMessage(record.ovSessionId, record.role === "user" ? "user" : "assistant", header, parts);
          record.status = "linked";
          record.linkedAt = new Date().toISOString();
          atomicWrite(recordPath(key), JSON.stringify(record));
          linked++;
        } catch (error) {
          failed++;
          record.lastError = String(error?.message || error).slice(0, 500);
          record.attempts = (record.attempts || 0) + 1;
          try { atomicWrite(recordPath(key), JSON.stringify(record)); } catch { /* keep going */ }
          log(`drain ${key.slice(0, 12)} failed: ${record.lastError}`);
        }
      }
      return { linked, failed };
    });
  }

  return { addImage, addFile, cursor, drain, directory: dir };
}

// ---------------------------------------------------------------- transcripts

export function readJsonl(path, fromLine = 0) {
  const lines = readFileSync(path, "utf8").split("\n");
  const total = lines.length && lines[lines.length - 1] === "" ? lines.length - 1 : lines.length;
  const start = fromLine > total ? 0 : fromLine; // file was rewritten: rescan
  const entries = [];
  for (let i = start; i < total; i++) {
    if (!lines[i].trim()) continue;
    try { entries.push({ line: i, value: JSON.parse(lines[i]) }); } catch { /* partial line */ }
  }
  return { entries, total };
}

const CC_WRITE_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);

function textOf(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(textOf).join("\n");
  if (value && typeof value === "object") {
    if (typeof value.text === "string") return value.text;
    if (value.content !== undefined) return textOf(value.content);
  }
  return "";
}

/** Claude Code JSONL -> artifact candidates. */
export function scanClaudeCode(entries, { cwd } = {}) {
  const images = [];
  const files = [];
  const mentioned = [];
  for (const { line, value } of entries) {
    const message = value?.message;
    const role = message?.role || value?.type;
    if (!Array.isArray(message?.content)) {
      if (typeof message?.content === "string") mentioned.push(...extractMentionedPaths(message.content));
      continue;
    }
    const entryCwd = value.cwd || cwd;
    message.content.forEach((block, blockIndex) => {
      if (block?.type === "image" && block.source?.type === "base64") {
        images.push({ role: "user", mime: block.source.media_type, data: block.source.data,
          identity: `${value.uuid || line}:${blockIndex}`, origin: { line, label: `cc-${line}-${blockIndex}` } });
      } else if (block?.type === "tool_result" && Array.isArray(block.content)) {
        block.content.forEach((inner, innerIndex) => {
          if (inner?.type === "image" && inner.source?.type === "base64") {
            images.push({ role: "user", mime: inner.source.media_type, data: inner.source.data,
              identity: `${value.uuid || line}:${blockIndex}:${innerIndex}`,
              origin: { line, tool_use_id: block.tool_use_id, label: `cc-tool-${line}-${innerIndex}` } });
          }
        });
        mentioned.push(...extractMentionedPaths(textOf(block.content)));
      } else if (block?.type === "tool_use" && CC_WRITE_TOOLS.has(block.name)) {
        const p = resolveAgentPath(block.input?.file_path || block.input?.notebook_path, entryCwd);
        if (p) files.push({ path: p, origin: { line, tool: block.name } });
      } else if (block?.type === "text" && role === "assistant") {
        mentioned.push(...extractMentionedPaths(block.text));
      }
    });
  }
  return { images, files, mentioned: [...new Set(mentioned)] };
}

const PATCH_FILE_RE = /^\*\*\* (?:Add File|Update File|Move to): (.+)$/gm;

/** Codex rollout JSONL -> artifact candidates. */
export function scanCodex(entries, { cwd } = {}) {
  const images = [];
  const files = [];
  const mentioned = [];
  let currentCwd = cwd;
  for (const { line, value } of entries) {
    const payload = value?.payload || {};
    if (value?.type === "session_meta" || value?.type === "turn_context") {
      if (typeof payload.cwd === "string") currentCwd = payload.cwd;
      continue;
    }
    if (value?.type !== "response_item") continue;
    if (payload.type === "message" && Array.isArray(payload.content)) {
      payload.content.forEach((part, index) => {
        if (part?.type === "input_image" && typeof part.image_url === "string" && part.image_url.startsWith("data:")) {
          images.push({ role: payload.role === "assistant" ? "assistant" : "user", dataUrl: part.image_url,
            identity: `${line}:${index}`, origin: { line, label: `cx-${line}-${index}` } });
        } else if (payload.role === "assistant" && typeof part?.text === "string") {
          mentioned.push(...extractMentionedPaths(part.text));
        }
      });
    } else if ((payload.type === "custom_tool_call" || payload.type === "function_call") && payload.name === "apply_patch") {
      let patch = payload.input || payload.arguments || "";
      if (typeof patch === "string" && patch.trim().startsWith("{")) {
        try { const parsed = JSON.parse(patch); patch = parsed.input || parsed.patch || patch; } catch { /* raw */ }
      }
      let m;
      while ((m = PATCH_FILE_RE.exec(String(patch))) !== null) {
        const p = resolveAgentPath(m[1], currentCwd);
        if (p) files.push({ path: p, origin: { line, tool: "apply_patch" } });
      }
      PATCH_FILE_RE.lastIndex = 0;
    } else if (payload.type === "function_call_output" || payload.type === "custom_tool_call_output") {
      mentioned.push(...extractMentionedPaths(textOf(payload.output)));
    }
  }
  return { images, files, mentioned: [...new Set(mentioned)] };
}

const PI_WRITE_TOOLS = new Set(["write", "edit"]);

/**
 * pi branch entries -> artifact candidates. pi's own image-capture already
 * handles image blocks durably, so only files and mentioned paths here.
 */
export function scanPi(entries, { cwd } = {}) {
  const images = [];
  const files = [];
  const mentioned = [];
  entries.forEach((entry, line) => {
    const message = entry?.message;
    if (entry?.type !== "message" || !message) return;
    const content = Array.isArray(message.content) ? message.content : [];
    content.forEach((block, blockIndex) => {
      // pi image blocks: user attachments and tool results (screenshots, read of a PNG)
      if (block?.type === "image" && typeof block.data === "string") {
        images.push({ role: message.role === "assistant" ? "assistant" : "user", mime: block.mimeType, data: block.data,
          identity: `${entry.id || line}:${blockIndex}`, origin: { line, label: `pi-${line}-${blockIndex}` } });
      } else if (block?.type === "toolCall" && PI_WRITE_TOOLS.has(block.name)) {
        const p = resolveAgentPath(block.arguments?.path, cwd);
        if (p) files.push({ path: p, origin: { line, tool: block.name } });
      } else if (block?.type === "text" && (message.role === "assistant" || message.role === "toolResult")) {
        mentioned.push(...extractMentionedPaths(block.text));
      }
    });
    if (typeof message.content === "string" && message.role === "assistant") {
      mentioned.push(...extractMentionedPaths(message.content));
    }
  });
  return { images, files, mentioned: [...new Set(mentioned)] };
}

/** Persist candidates locally (no network). Returns number of new records. */
export function stage(store, ovSessionId, scan, log = () => {}) {
  let added = 0;
  for (const image of scan.images) {
    try {
      const decoded = image.dataUrl ? decodeDataUrl(image.dataUrl) : decodeBase64Image(image.mime, image.data);
      if (store.addImage({ ovSessionId, role: image.role, ...decoded, identity: image.identity, origin: image.origin })) added++;
    } catch (error) { log(`image ${image.identity}: ${error.message}`); }
  }
  const seen = new Set();
  for (const file of scan.files) {
    if (seen.has(file.path)) continue;
    seen.add(file.path);
    if (store.addFile({ ovSessionId, role: "assistant", path: file.path, origin: file.origin })) added++;
  }
  for (const path of scan.mentioned) {
    if (seen.has(path)) continue;
    seen.add(path);
    if (store.addFile({ ovSessionId, role: "assistant", path, origin: { mentioned: true } })) added++;
  }
  return added;
}

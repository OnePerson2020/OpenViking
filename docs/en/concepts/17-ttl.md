# Directory TTL

TTL is off by default. It covers user and peer `events/YYYY/MM/DD` directories and `sessions/{session_id}`. Resources and other memory categories are outside this scope.

## Configuration and policy application

Configure library defaults, type defaults, or one of these exact policy roots:

- `viking://user/{user_id}/memories/events`
- `viking://user/{user_id}/peers/{peer_id}/memories/events`
- `viking://user/{user_id}/sessions`

Priority is concrete root → type (`user_events`, `peer_events`, `sessions`) → library global → disabled. `disabled` stops inheritance; `inherit` falls back. Existing account configuration represents the library; user/account identity adds no policy level.

Years, months, dates, individual sessions, nested directories and files expose read-only deadlines. Enabling or changing a root policy also updates existing live directories, including previously unmanaged history, while respecting more-specific overrides. Relative expiry uses the original business timestamp plus the new duration; absolute expiry uses the configured deadline. Shortening may expire a directory immediately. Disabling the effective policy clears live deadlines. Expired and deleted objects are never revived.

The configuration request pages through directories, limits concurrency, and awaits metadata and expiry-index writes. Partial failures report incomplete directories; retry the same configuration. For relative policies, history without a reliable original timestamp is reported rather than assigned the configuration update time or directory modTime. Explicit empty event directories can be created and start their lifetime on the first body write.

## Lifetime and renewal

Each lifecycle directory stores one `expires_at` in `.meta.json`, plus `ttl_days` for relative retention. `received_at` records the content time. Events can still read legacy `.ttl.json`. AGFS directory metadata updates preserve other business fields in the same file; directory stat exposes its own deadline.

- Events start their lifetime on the first successful content write. The path date only groups events. Later body writes never renew deadlines; explicit root policy changes can adjust live directories.
- Sessions inherit their root policy on creation. Relative policies renew after successful message appends and completed nonempty commits using the saved `ttl_days`, which explicit root policy changes also update. Replays use the original completion time.
- Reads, summaries, reindexing, failed writes, empty message batches and empty commits do not renew. Absolute deadlines never renew automatically.

Messages, attachments, archives and L0/L1/L2 share the directory deadline. There is no message-level JSONL retention, `ttl_generation`, per-session override or per-file mode.

## Visibility

At `now >= expires_at` in UTC, the directory and all descendants become invisible. Direct reads return 404. Session/file listings, find/search/recall, grep and glob filter expired content and refill visible candidates.

Structured objects expose their owner's `expires_at`, explicitly `null` without TTL. Mixed results carry per-item deadlines. Single-owner text/list responses include the deadline in the envelope. URI-only listing compatibility modes and download bytes keep their existing shape and still enforce server filtering. Root/year/month containers have no shared expiry; policy roots also expose `policy` and `effective_policy`.

## Cleanup and performance

Cleanup uses the existing Session commit QueueFS worker framework. The scheduler claims candidates from the durable expiry index with count, byte and time budgets. Physical deletion is spread over a day-scale window by default.

The worker rechecks the owner deadline under its metadata file lock and reuses the existing Session mutation mutex. It deletes files under individual exact locks, retries contention, and uses no tree lock or per-file expiry decision. It removes all owned bodies, messages, attachments, L0/L1, vectors and Meta. Metadata is removed last; registration is removed after storage and index verification. External parent summaries stay unchanged. Deletion triggers no LLM, embedding or summary rebuild.

Result batches share owner metadata reads. Retry state lives in the expiry registry, independently of task-history retention. Display and billing may lag physical deletion. OV cleanup alone does not verify cloud billing, gateway forwarding or backup erasure.

## Interfaces

- [TTL configuration](../configuration/01-server.md#ttl): library/type/root policies.
- [Expiry query](../api/12-content.md#document-expiry): `GET /api/v1/content/ttl`, SDK/MCP `get_ttl`, CLI `ov ttl get`.
- [Sessions](../api/05-sessions.md#session-ttl): create/config APIs inherit the root policy and accept no TTL input.

Asynchronous Session commit writes validate the original Phase 1 `task_id` under a short Session lock. Old work cannot modify a replacement Session with the same ID or recreate events from a deleted source. A fresh Session may still import an older calendar date. Ordinary body I/O releases the common metadata lock; cleanup blocks new admissions and checks existing file leases, including writes whose file does not yet exist.

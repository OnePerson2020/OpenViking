# TTL lifetime and renewal

## Expiry contract

TTL is off by default. An object with relative retention stores its effective
`ttl_days` and `expires_at`. A successful content update renews its deadline to
the update time plus the same `ttl_days` (N × 24 hours). Reads, searches, reindex,
failed writes and empty session commits do not renew retention.

An absolute deadline stores `expires_at` with no relative `ttl_days`. Content
updates do not move that deadline. A user can explicitly change a live object's
deadline; there is no automatic 30-day extension or conversion of an absolute
deadline into a duration. Expired objects cannot be revived by an update.

`ttl_relative` and `ttl_absolute` are input parameters. They are normalized into
the effective lifetime above. This does not eliminate configuration state or
concurrency metadata: the current session metadata also retains `ttl_relative`
to represent its explicit configuration override, while `ttl_days` is the frozen
effective duration. `received_at` remains the compatible name for the content
time used to calculate relative expiry. Generation checks and cleanup registry
records protect against stale deletion and delayed writes; none of these
lifecycle fields requires new public-cloud vector schema columns.

## Incremental defaults

New objects resolve the nearest applicable directory policy, then the scope
default, then the library default. Resources use their independent scope default
and never inherit the library-global TTL. Explicit object settings take priority.
`inherit` continues resolution; `disabled` stops it.

Changing library or directory defaults, including disabling them, does not
rewrite existing lifetimes or adopt unmanaged historical objects. Existing
relative objects continue renewing with their own frozen duration. Explicit
single-object retention edits can configure a live unmanaged object or change
an existing lifetime.

## Session boundary

A session uses a whole-session lifetime by default. Successful message appends
and completed content-bearing commits renew its relative lifetime. This also
extends the lifetime of existing attachments governed by that same session.
The formal session create/config APIs accept relative TTL only; this change does
not introduce absolute TTL parameters for sessions.

On expiry, normal session access fails and L2 content is hidden. Background
cleanup removes `messages.jsonl`, archived message bodies, attachments and their
L2 vectors. It deletes whole files, not selected JSONL messages. All L0/L1
summaries, their vectors and supporting directories remain visible and stored.
Messages may remain physically present between logical expiry and cleanup; they
are not retained indefinitely by this design.

Existing explicit session child-file and child-directory retention remains
supported. Configuring it migrates a live session to per-file lifetimes without
moving existing deadlines. In that mode a changed file does not renew sibling
files, and `messages.jsonl` is still one expiry unit. This is not message-level
TTL, and changing a session default does not rewrite existing child snapshots.

## User-visible retention

For a file, display its effective `expires_at` as the expiry time; no deadline
means retention is not enabled for that object. Relative retention can also show
the configured duration. This timestamp is logical expiry, not a promise that
physical deletion has completed at that instant.

A policy directory displays its retention duration and inheritance/disabled
state, not one shared `expires_at`: its children can expire at different times.
A whole-session object can display its session deadline even though its storage
is a directory. Per-file session mode has no single root expiry.

## Cleanup and acceptance

Visibility uses the current `expires_at`. Physical cleanup is asynchronous and
spread over a day-scale scheduling window. Workers recheck the live generation
and deadline under the object lock before strict deletion; failures retain
retry state. Scheduling delays are not a guaranteed deletion or billing SLA.

Local acceptance covers configuration, relative renewal, fixed absolute expiry,
read filtering, strict deletion and retries. Console configuration/presets,
actual page behavior and billing reconciliation require separate integration
validation. Storage deletion alone is not proof that billing has caught up.

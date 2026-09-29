# Directory TTL

TTL is off by default. The lifecycle unit is one **event date directory**
(`events/YYYY/MM/DD`) or one **session directory**. User and peer event trees
use the same rule. Resources and other memory categories have no TTL.

## Lifetime and renewal

Every lifecycle directory owns one `expires_at`. Its L2 descendants inherit
that deadline; files and nested directories cannot override it. Relative
retention also stores `ttl_days`. A successful content change renews the
whole directory to the update time plus that duration (N × 24 hours).

For events, the date in the path groups events; it is not the TTL start time.
Adding or changing an event renews the relative lifetime of its date directory.
A session renews after a successful message append or a completed commit with
content. Reads, searches, summary generation, reindexing, failed writes and
empty commits do not renew TTL.

An absolute deadline stays fixed through content updates. A user can explicitly
change a live event directory's deadline. There is no automatic 30-day extension.
Session create/config APIs support relative TTL only. Expired directories cannot
be revived by a delayed write or a TTL edit.

`received_at` stores the content timestamp used for relative expiry.
`ttl_generation` fences delayed cleanup and indexing work after deletion and
recreation. Session metadata also retains `ttl_relative` as its explicit
configuration override. TTL needs no extra public-cloud vector schema fields.

## Configuration and incremental defaults

New directories resolve the nearest explicit directory policy, then their type
default (`user_events`, `peer_events`, or `sessions`), then the library-global
policy. Explicit `disabled` stops inheritance; `inherit` continues upward.

Library defaults use the existing account configuration layer. Account and user
identities do not add extra TTL priority levels. Directory defaults can address
an events root, year, month or date, or a user's sessions container.

Changing defaults only affects newly created lifecycle directories. Existing
managed directories keep their frozen duration, and previously unmanaged
directories remain unmanaged, including new files written into them. A live
directory can be explicitly configured through the retention endpoint.

## Visibility and cleanup

At `expires_at`, L2 content is hidden from normal session access, file reads,
listings, find, search, grep and glob. Files and directory details expose the
shared `expires_at` and `ttl_days`; no deadline is returned as `expires_at: null`.
Policy containers have no common deadline and expose `policy` and
`effective_policy` instead. Summary files always have `expires_at: null`.

Cleanup checks the **directory's** live deadline and generation under a directory
lock. Once due, it removes all L2 descendants and L2 vectors without checking
individual file deadlines. For sessions this includes whole `messages.jsonl`
files, archived messages and attachments. It does not manage individual JSONL
messages.

**All L0/L1 summary files, their vectors and the directories needed to hold them
remain unchanged, readable and searchable.** Removing expired L2 does not
regenerate summaries. A retained summary can still describe expired content.

Physical cleanup is asynchronous, spread over a day-scale window by default.
Failed deletion or incomplete verification retains retry state. Messages can
remain physically stored between expiry and cleanup. Cleanup confirms primary
L2 storage and index removal; it does not confirm a console refresh, backup
erasure or a billing adjustment. Retained summaries still occupy storage.

## Interfaces

- [TTL configuration](../configuration/01-server.md#ttl): library/type/directory defaults.
- [Directory retention](../api/12-content.md#document-expiry): query deadlines and edit a live date directory or session.
- [Sessions](../api/05-sessions.md): create and update relative retention.

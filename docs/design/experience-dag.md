# DAG experience memory

Experience templates stay at `viking://user/{user}/memories/experiences/{name}.md`.
The `content` field contains the complete DAG construction source. Python is never executed
with `exec` or `eval`; a restricted interpreter validates and builds the runtime DAG.
Existing text experiences are not converted or accepted as DAG templates.

## Extraction and updates

Agent Evolution learns directly from Sessions. It does not generate or persist Trajectory
memories. Each canonical Case owns one Experience with the exact same name; trial-specific
Session names do not rename that Case. This applies to ordinary sessions as well as benchmarks.

```text
Session + canonical Case + evaluation + runtime execution feedback
  -> ExperienceGradientEstimator: one proposal for the fixed Case Experience
  -> Jev proposal Gate: replay this proposal against this Session
  -> streaming buffer: accepted concurrent Session proposals
  -> PatchMerge: group by Experience URI, rebase against the latest stored version
  -> deterministic DAG and fixed-target validation
  -> persist Experience + Session provenance + Case relation
```

The estimator reads only that Case's Experience. It reflects on raw conversation and tool
results, external evaluation (when available), and factual runtime snapshots. Model output
cannot rename, supersede, delete, split, or update another Case's Experience. The Experience
body remains complete standalone Python source. For example:

```python
sdk.create_experiences(
    experience_name="check_order",  # exact canonical Case name
    content='''dag = workflow("User asks about an order")
order_id = ask("What is the order ID?")
order = call("query_order", "Use order_id from context")
order_id.then(order)
''',
)
sdk.commit()
```

Sessions generate proposals concurrently without waiting for every trial. Each proposal passes
the replay Gate against its own Session before entering the streaming buffer. Count/time buffering
can produce several updates to the same Experience across a run. Each flush groups accepted
proposals by fixed URI before PatchMerge. The policy root lock covers reload, merge and apply, so a
stale proposal is merged against the latest Experience instead of overwriting concurrent improvements.
Case links use the same policy lock boundary when their backlinks are updated.

For updates, the model reads the old source and emits a complete replacement program. Every
program begins with `dag = workflow(...)`, declares nodes through `ask/call/check/tell/choose`,
and adds relationships through object references. Node variable names stay stable for unchanged
behavior. The compiler generates internal IDs.

The interpreter permits literals, lists, dictionaries, variable assignments and the listed
constructors/methods. Imports, loops, arbitrary calls, attribute reads and computation are
rejected. Programs and serialized graphs are limited to 256 KiB; graphs have at most 256 nodes.

Validation runs after the complete program. The final graph must be nonempty, acyclic, have distinct slot names and positive
IDs, and have matching branch targets/predecessors. Single-node templates are valid.
Invalid extraction output gets one repair attempt. The optimizer uses the same construction
and validation path. Storage validates again before accepting replacements or related deletes.
The source body bypasses Markdown content templates and linkification; relations remain in
`MEMORY_FIELDS` metadata.

## Server-side execution

Create a normal OV session and send the current conversation and tool-result evidence to the
automatic recall endpoint:

```http
POST /api/v1/sessions/{session_id}/experiences/search
Content-Type: application/json

{
  "context": "The user supplied order ID 123. No query_order result exists yet."
}
```

The server searches the current user's Experience directory, keeps the top matching templates,
restores their session-scoped instances, decides node states and returns only their current
`instructions`. Detailed per-Experience transitions remain in `experiences` for observability.
Callers may provide bounded evidence records with `id`, `kind` and `summary`; unknown citations
reject the corresponding Experience update. Suggesting an action does not mark it complete:
unfulfilled actions remain available on later requests. Individual evidence summaries may carry
up to 16,384 characters rather than the former 4,096-character limit. The final Jev state is still bounded by
the configured aggregate budget; structured tool evidence is clipped without dropping its
`tool_name`.

Jev is configured as a reusable top-level decision service, while Agent Evolution only selects
it as the DAG decision provider:

```json
{
  "jev": {
    "api_url": "https://api.typesafe.ai/v1/systemone",
    "api_key": "...",
    "model": "jev-latest",
    "max_input_tokens": 28000,
    "timeout": 30,
    "verify_ssl": true
  },
  "agent_evolution": {
    "dag_decider": {
      "provider": "jev",
      "noul_true_threshold": 0.7,
      "noul_false_threshold": 0.3,
      "choice_confidence_threshold": 0.5
    }
  }
}
```

One `search_exp` call translates unresolved nodes across active Experience instances into typed
System One questions. Questions are kept in one request while the conservative estimated input is
within `max_input_tokens`; larger sets are bisected without duplicating or truncating questions.
An HTTP 413, or a 422 explicitly reporting a context-length/input-token overflow, triggers the
same split. Boolean nodes become Noul questions and conditional branches become Choice questions.
Jev never generates business values: identifiers and tool-result objects stay in the evidence
table. Missing, malformed or uncertain answers leave nodes pending. Transient Jev failures do not
invalidate the Experience and never fall back to slow generative slot filling when the Jev
provider is selected.

The in-repository HTTP client also provides:

```python
result = await client.search_exp(
    session_id,
    context="User supplied order 123; query_order has not run yet.",
)
```

Agent integrations call this automatically before each model decision. Recall and DAG loading
do not appear in the model-visible tool list or execution trajectory. The server never executes
the business tools described by `CallTool`.

Memory retrieval is control-plane behavior and is never represented as a DAG node. In
particular, `call("search_exp")`, `call("search_experience")`, and
`call("read_experience")` are rejected by the compiler.

### Tau2 integration

The Tau2 VikingBot executor calls server-side `search_exp` automatically before every model
decision. The server directly searches Experience memories and returns only selected DAG
instructions; recall is not exposed as an agent tool. The executor replaces the previous
guidance with the new instructions and uses a unique runtime session for every rollout.
Runtime events are included in rollout memory artifacts (and `dag_runtime` metadata). Each
transition stores the bounded evidence table, newly completed nodes with their cited evidence
IDs, and the lifecycle of prior actions (`issued`, `pending`, `completed` or `superseded`).

When a rollout is committed, `experience_execution` carries the final runtime snapshot keyed
by Experience URI: revision, state, slot values and evidence citations, executed/current nodes,
actions, completed nodes, and action outcomes. Node arrays use stable `slot_name` values, which
are the Python variable names. These observations are passed directly to the reflection stage.
They are not independently verified business outcomes; the external Session evaluation remains
separate. Missing evaluation is represented as unknown, never converted into a passing result.

The proposal Gate compiles one Session's candidate and its currently stored baseline, then starts
fresh DAG instances against that Session's original evidence. Corrective-action and preservation
judgements share one Jev request, retaining the candidate/baseline pair as one unit when splitting
over-budget requests. A rejected proposal never enters PatchMerge and does not reject another
Session's proposal.

For each Session proposal, acceptance requires:

- A successful source Session still completes its path and preserves grounded obligations.
- A failed source Session is non-regressing and has a materially better corrective action. Merely
  labelling its failed execution complete is not improvement.
- Unknown outcomes require an evidence-grounded, non-regressing workflow; they never count as
  proof of success or as the required failed-Session improvement.
- New/all-success Experiences need successful replay and grounded obligations, without a
  fabricated failed sample. Missing source evidence or replay errors reject publication.

PatchMerge does not run a second Jev semantic Gate in this version. It still compiles and validates
the complete merged DAG, enforces the fixed Case name and URI, rejects deletes/renames, and rebases
under the current policy lock. Independently valid proposals can therefore conflict semantically
after merge; this is an explicit current limitation.

Accepted Experiences store forward `derived_from` links to Session archive directories and a
`source_sessions` metadata list of source identity, evaluation and feedback. Raw evidence is not
duplicated into Experience metadata. Archive contents are never rewritten to add memory backlinks. Snapshot
messages use `OpenViking-Experience-Session-Map`; submitter memory diffs, Case relations and Gate
diagnostics are scoped to the originating Session and Case.

Optional skill extraction runs separately and does not generate Trajectory memories. An explicit
account-level Agent Evolution `false` disables Case/Experience learning. Tau2's no-memory mode
also disables automatic Experience recall. Training errors propagate to the owning commit task;
Gate rejections remain explicit diagnostics in the memory diff.

## Semantics and state

- A source may fan out to many nodes with `source.then(a, b)`. Multiple `.then(target)` edges
  pointing to the same target form an **AND** join: the target waits for every source node.
- All non-choice slots are completion booleans suitable for a Jev Noul decision. AskUser,
  CallTool, TellAgent, Check, and Parallel nodes advance only on `true`; actual user answers and
  tool-result payloads remain in conversation evidence rather than DAG slot state.
- IfElse slots are booleans; a selected null target terminates that branch.
- Conditional slots select one exact `.case(...)` label. The reserved `__default__` label selects
  a configured default target; arbitrary unmatched strings are rejected.
- All slot values are validated before updating state; unknown slot names are rejected.
- Instance identity is `(account, user, session, experience_uri, instance_id)`. This release
  only accepts `instance_id="default"`; the model keeps the instance dimension for extension.
- Instances snapshot their DAG at creation. Template updates affect new sessions, not an
  in-progress instance. State is saved below the session's `.experience_instances/` directory,
  so service restarts preserve progress and session deletion removes it.
- Decision-service calls happen outside the storage lock. A comparison under the authoritative path lock
  rejects stale concurrent updates with `CONFLICT` (HTTP 409); callers may retry with current
  context. Model or validation failures do not persist a partially advanced instance.
- Multi-instance creation/selection, automatic conversion of legacy text, and tool execution
  are outside this release.

## Checks

```bash
.venv/bin/pytest tests/session/memory/test_experience_dag.py tests/service/test_experience_runtime.py -q --no-cov
```

These checks include the Python extraction protocol, repair, graph materialization, file
serialization and server runtime in one flow, using a mock VLM and storage. They also cover
invalid programs/graphs, branches, default-instance validation, restart recovery, isolation,
and concurrent state updates. They do not measure live-model slot-filling accuracy.

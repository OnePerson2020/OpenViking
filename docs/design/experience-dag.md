# DAG experience memory

Experience templates stay at `viking://user/{user}/memories/experiences/{name}.md`.
The `content` field contains the complete DAG construction source. Python is never executed
with `exec` or `eval`; a restricted interpreter validates and builds the runtime DAG.
Existing text experiences are not converted or accepted as DAG templates.

## Extraction and updates

The existing extraction protocol still identifies the experience, handles its URI and
`supersedes`, and records trajectory provenance. The experience `content` output is complete
Python source. For example, with the Python extraction protocol:

```python
sdk.create_experiences(
    experience_name="check_order",
    supersedes="",
    content='''dag = workflow("User asks about an order")
order_id = ask("What is the order ID?")
order = call("query_order", "Use order_id from context")
order_id.then(order)
''',
)
sdk.commit()
```

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
up to 16 KiB rather than the former 4096-character limit. The final Jev state is still bounded by
the configured aggregate budget; structured tool evidence is clipped without dropping its
`tool_name`.

Jev is configured as a reusable top-level decision service, while Agent Evolution only selects
it as the DAG decision provider:

```json
{
  "jev": {
    "api_url": "https://example.com/v1/systemone",
    "api_key": "...",
    "model": "qwen3-1.7b",
    "max_input_tokens": 110000,
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

When a rollout is committed, every extracted Trajectory stores an `experience_execution` JSON
field keyed by canonical Experience URI. The value is the latest execution snapshot for that
Experience: state, revision, slot values and citations, ordered executed nodes, current nodes,
current actions, completed nodes, and action outcomes. This mirrors the reference implementation's
per-SOP `execute_path`: training can identify the concrete Experience and earliest incorrect DAG
step instead of inferring the path from conversation text. The field is populated by the server
from `dag_runtime`; extraction output cannot author or override it. Raw evidence messages are not
duplicated into this field. Node arrays use stable `slot_name` values, which are the Python source
variable names; compiler-assigned numeric node IDs remain only in the detailed action records.

The batch commit protocol compacts `dag_runtime` into one final snapshot per Experience before
crossing the HTTP/session boundary. Experience training stores those factual snapshots on each
Trajectory and supplies the same `dag_execution_feedback` to `ExperienceGradientEstimator`
alongside the trajectory and outcome. This makes a failing update attributable to a concrete
node/slot/action path instead of a generic trajectory summary. Citations establish a bounded
model claim about the evidence that supported a slot; they do not independently prove factual
correctness.

`ExperienceGradientEstimator` is the reflection stage. It produces a complete candidate
Experience, but that candidate is not published immediately. After patch merge, the Experience
improvement gate compiles the final candidate and, for an update, its stored baseline. It replays
both from empty instances against the source Trajectory's original conversation and tool-result
evidence using Jev. DAGs sharing the same normalized evidence are decided together. Failed-path
relevance checks keep each candidate and its baseline as one indivisible unit, then pack those
units under the configured Jev budget. Provider-reported overflows are bisected; a single
oversized candidate is rejected without rejecting unrelated candidates. For a successful
Trajectory, the candidate must still complete. For a failed Trajectory, the candidate must stop
before accepting the same failure and expose a current action that Jev judges both directly
relevant to the evaluation feedback and, when a baseline exists, materially better than the
baseline replay. The runtime
`experience_execution` snapshot remains provenance and diagnostic input; the replay from original
evidence is the publication authority. Validation is scoped to each candidate Experience and its
linked Trajectories. Passing candidates may be applied when an unrelated candidate fails; a
supersede delete is applied only with its accepted replacement. Unattributed merge-only deletes
require every candidate in that merged plan to pass. Gate diagnostics remain on the training plan
for inspection.

Agent Evolution is enabled by default. An account-level explicit `false` still disables
Case/Trajectory/Experience learning for that account. Tau2's no-memory mode also disables
automatic Experience recall, so those rollouts make no DAG calls.

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

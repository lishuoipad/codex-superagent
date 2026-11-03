# Runtime integration

The runtime stores routing state in its own files. Product progress and acceptance remain in the original project records. Python 3.10+ and the standard library are sufficient for local routing and tests.

Detailed schemas and lifecycle guidance are also available in [中文](runtime.zh-CN.md).

## 1. Configure an explicitly authorized team

Start from [the example](../assets/runtime-config.example.json). Replace every placeholder with current, verified values. Use absolute local paths for database, registry, status, authorization and command inputs. Record the actual human instruction in a private authorization file and compute its SHA-256.

The cap is project configuration. Waiting, paused and unfinished repair tasks count; the Router and completed, released history do not. Initial development must be false.

~~~text
python /absolute/scripts/task_runtime.py --config /absolute/local-config.json init
python /absolute/scripts/task_runtime.py --config /absolute/local-config.json status
~~~

Initialize a new deployment once. Never delete or recreate the database/registry to clear an unresolved event. The configuration hash is bound to the store; do not edit it as an informal way to add Workers. Use `assign` with the real new identity and authorization instead.

Commands are `init`, `status`, `export`, `ingest`, `subscribe`, `assign`, `complete`, `pause`, `resume`, `worker-state` and `reconcile`. Mutation payloads use an absolute `--input` JSON file, up to 64 KiB.

An assignment payload has `actor`, `worker={threadId,taskBindingId,title}` and `authorization={path,sha256}`. The actor is the configured Router. Team pause/resume uses `actor` and `authorization`; per-task state also supplies `threadId` and `state`. These are local routing controls, not native Goal changes or interrupted-tool cancellation.

**The trusted calling host validates human authority.** An actor string, a file hash or a caller-supplied boolean is not authentication.

## 2. Ingest stable events

An event contains `eventId`, an allowed `source`, `kind`, and a bounded `summary`. Known-task events identify `taskBindingId` or a previously bound `sourceKey`; optional `evidenceRef` is a local path/hash reference.

- `followup`, `dependency_ready`, `result` and `background_completed` route to the bound live task owner.
- `new_request`, `out_of_scope` and `human_action` route to the Router.
- Event identity must remain stable across retries. Reusing an ID with different content is rejected.
- Subscriptions do not install an event producer. The actual host must submit events.

Ingest records a destination and pending intent. It sends no native message and cannot resume development.

## 3. Native-chat delivery

When your Codex host provides an authorized native messaging tool, `task_native_delivery.py` audits its lifecycle in the existing runtime store:

~~~text
prepare -> begin -> actual host message tool -> finish -> recipient receipt
~~~

Use the script's `--help`; each operation takes `--config` and a bounded `--input`.

`prepare` receives operator, event, exact prompt and authorization. `begin` receives operator, sessionToken, eventId and a fresh snapshot. `finish` receives the attemptToken, actual status and tool evidence. `receipt` records the recipient, task binding, consumer turn and a verified evidence reference.

Preserve the actual tool's authorization requirements and recheck pauses immediately before sending. The adapter itself does not invoke MCP tools. Local admission and the later native message call are separate operations; this is not an atomic pause guarantee.

Unknown or expired in-flight outcomes require inspection of original host evidence, followed by explicit `reconcile`. Never switch providers or change an event ID to replay an uncertain send.

## 4. Select the next work from the existing plan

~~~text
python /absolute/scripts/project_continuation.py inspect --plan /absolute/plan.json --ledger /absolute/ledger.json --team /absolute/team.json --runtime-config /absolute/local-config.json --scope PhaseA,PhaseB
python /absolute/scripts/project_continuation.py next --plan /absolute/plan.json --ledger /absolute/ledger.json --team /absolute/team.json --runtime-config /absolute/local-config.json --scope PhaseA,PhaseB
~~~

Only include human-authorized phases. The selector is read-only and sends nothing. It reads current runtime bindings and the existing plan/ledger, separating development and acceptance dependencies. Unknown or stale evidence becomes an Owner review request.

The technical Owner maintains `continuation.readyNextTasks` in the existing ledger. Declarations include taskId, mode, planUnitSha256, reviewedBy and evidenceRef. Use the canonical plan-unit hashes returned by `inspect`, not raw file hashes. See [the detailed field guide](runtime.zh-CN.md#原计划接续) and the continuation tests for current schemas.

Accepted or consumed intents must leave the current ready list. Keep their history. Reassign only after the previous complete task is properly accepted; do not turn a substage delivery into free capacity.

A final completion review binds current plan scope and required task IDs, with no unresolved applicable work. A selector's completion result depends on supplied evidence; the technical Owner remains responsible for its meaning.

## 5. Host-dependent continuous operation

Normal real host events should deliver known-task changes directly to the Owner. A user-authorized native follow-up in the Router chat may catch missed events. Verify actual wakeup, routing, deduplication and consumption; keep unchanged waits quiet and stop the follow-up at project completion or human pause.

The optional `task_runtime_host.py` connects only through `codex app-server proxy --sock` to an existing compatible host. It offers `--check`, `--once` or bounded `--duration-seconds` (1–900). Actual sends require a matching `--verification` receipt with the exact configuration hash, executable, socket and reviewed evidence.

The reference desktop did not expose that compatible socket. This adapter has local tests but is not a generally verified desktop connection. Never start a second backend to load the same chats or use a private-IPC/queue fallback. No helper installs a service or renews its own lifetime.

Keep one dispatcher/provider per event. The native-chat path and optional RPC path have separate verification evidence and must not both send the same event.

## Worker titles follow accepted tasks

In an adopted Router / complete-task Worker team, the Router is the single writer of task titles. On assignment, store the stable project/Worker prefix plus the short complete-task name in the existing binding's `title`. Reassignment preserves an existing `【…】` or `[…]` prefix. Substeps, waits, repairs and reservations do not rename an unfinished binding or require a new Goal.

A native `receipt` for the current binding returns a `titleSync` hint. It is not evidence that a title changed. Read the real title with `list_threads`, then compare it using the existing adapter:

```text
python /absolute/scripts/task_native_delivery.py --config /absolute/runtime-config.json title-sync --input /absolute/native-titles.json
```

Input: `{"snapshots":[{"threadId":"worker-id","hostId":"host-id","source":"codex","title":"observed native title","observedAt":0}]}`. Use the actual read's UTC seconds. The existing 15-second fresh-snapshot window applies; refresh an expired read without resending the task. Only current, unfinished bindings with a consumed native event of the same generation can produce a rename action. Completed bindings and late old-task receipts cannot rename a new task. Title metadata does not require an active Worker to stop its tools.

For `rename_required`, the authorized Router rechecks the current binding and latest human instructions, calls the returned `set_thread_title` arguments, then reads back the real title and compares again. Matching titles are no-ops. Save the binding/generation, intended title, actual tool result and readback in the existing handoff/team record. On failure or uncertainty, read back first and finish at the next normal handoff or existing scheduled follow-up; do not duplicate dispatch or block development.

The comparison command never invokes MCP, changes routing state, Goals, models or pauses. Separate native calls are not atomic, and a plan is not authorization or proof of execution. Workers confirm the task in their ordinary receipt; the Router owns the sidebar rename, including tasks that do not use Goals.

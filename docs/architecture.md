# Architecture: SuperAgent ideas, Codex interfaces

Codex SuperAgent adapts the Router / complete-task Worker pattern to the interfaces available in Codex. Its implementation comes from Agent Relay; it does not bundle or launch the DeepSeek Harness.

## The responsibility boundary

A Router preserves the user's request, chooses ownership and coordinates cross-task dependencies. A Worker owns the complete technical outcome. Technical investigation, design, source edits, shared wiring, tests and repairs remain with that Worker.

This keeps a phone test, build or long technical investigation from turning the Router into a queue for unrelated work. It does not remove real resource dependencies or authorize two writers on the same file.

Existing-task events retain their owner. A new task or ownership conflict goes to the Router. Internal subagents stay inside their parent's task and host quota.

## Components shipped here

| Component | Implementation | What it does |
| --- | --- | --- |
| Role workflow | [SKILL.md](../skills/codex-superagent/SKILL.md) | Establishes responsibility, authorization, evidence and pause rules. |
| Durable runtime | [task_runtime.py](../skills/codex-superagent/scripts/task_runtime.py) | Bindings, worker capacity, generation, subscriptions, deduplication and dispatch state. |
| Native-tool audit | [task_native_delivery.py](../skills/codex-superagent/scripts/task_native_delivery.py) | Records intent, native tool outcome and recipient consumption. |
| Plan continuation | [project_continuation.py](../skills/codex-superagent/scripts/project_continuation.py) | Reads original plan/ledger and proposes eligible work or an Owner review. |
| Optional RPC host | [task_runtime_host.py](../skills/codex-superagent/scripts/task_runtime_host.py) | Bounded delivery through an existing, verified compatible control socket. |
| Local dashboard | [development-status](../tools/development-status/README.md) | Displays recorded team, task, subagent and public progress data. |

The SQLite store and RPC transport preserve their Agent Relay module names. The delivery-reference verifier keeps its bounded JSON reader locally, so this focused package does not load the former supervision/recovery workflow. The SuperAgent workflow does not create a monitoring agent.

## Persistence is not a second product ledger

Routing state tracks ownership, task generation, stable event IDs, delivery intent, pauses and receipt evidence. Existing project records remain authoritative for implementation and acceptance facts. The technical Owner maintains them.

A hash confirms referenced bytes. It cannot establish human authorization, visual correctness or semantic acceptance. The trusted caller checks those meanings.

The Worker cap is explicit configuration. A paused or waiting unfinished task still occupies a slot. A completed, released historical chat does not. No slot automatically creates a chat or grants subagent quota.

## Verification boundaries

There are four separate questions:

1. Do the instructions assign the right responsibilities?
2. Do the local runtime checks behave correctly?
3. Does the real host deliver an authorized event to the intended chat?
4. Did the Worker produce the required technical result?

Local tests answer the second question. The reference deployment has separately exercised native-message wakeup, handoff, consumption and subsequent work. Its private operational records are not distributed. Users must validate host integration in their own environment.

The native-tool adapter spans multiple calls and cannot promise an atomic pause barrier around the actual send. Router role instructions are not a host-wide tool permission sandbox. The optional RPC adapter requires a verified existing control interface and is not generally connected to Codex Desktop by installation.

Model selection is an authorized human/tool decision, not an automatic property of a task name or Goal. Multi-device switching, automatic GitHub merging and permission bypasses are outside this implementation.

## Relationship to DeepSeek and Agent Relay

DeepSeek's [architecture reference](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/007-architecture.md) describes its own Runtime, Engine, Store and Harness integration. This project carries over the role separation and persistent event/intent ideas using different Codex-facing components. It is not a source-compatible port of that runtime.

[Agent Relay](https://github.com/lishuoipad/agent-relay) also supports a traditional combined coordinator. This distribution makes the separated Router / complete-task Worker workflow the skill's focus. Existing installations and running teams are not migrated automatically.

# Architecture and responsibility boundaries

Codex SuperAgent combines a project-coordinating Router, complete-task Workers and durable event delivery. It grew from Agent Relay's implementation, using Codex host interfaces.

## Project-level decomposition belongs to the Router

The Router owns how a broad project goal becomes workable tasks. It checks requirements, actual entry points, shared state, write ownership, dependency order, resource constraints and independently verifiable delivery boundaries. It can inspect the relevant code or ask a Worker for a bounded investigation, then decides whether to split, combine or sequence the work.

Workers own implementation planning inside their assigned tasks, including detailed design, code, shared wiring assigned to them, affected validation and repairs. Known-task follow-ups return directly to their Owner. A new fact changing project boundaries or dependencies returns to the Router.

For example, the Router decides whether mail and inventory changes can proceed separately and who owns common session state. The mail Worker decides how its screen handles state, interfaces, errors and validation. The existing project plan and technical evidence support both decisions.

This separation keeps project-level technical judgment with one accountable coordinator while ordinary in-task decisions stay with the executor. Ongoing project splitting is reviewed at meaningful boundary changes and normal planning points.

## Coupling and delivery boundaries

Use the criteria in [project-level decomposition](../skills/codex-superagent/references/workflow.md#project-level-decomposition).

- Changes sharing mutable state, a tightly changing interface or a continuous device/save sequence can stay with one Owner or run in an explicit dependency order.
- Independent implementation inputs, write scopes and useful deliverables support parallel work.
- Later device acceptance can have a different dependency from implementation. Record both the delivered work package and the remaining acceptance Owner.
- A scoped investigation can resolve an uncertain boundary before the Router decides.
- Review existing delivery, waiting and repair evidence to refine the split.

These are coordination decisions made from technical facts. The local selector checks recorded dependencies and references. The Router judges whether the proposed work units are cohesive.

## Relationship to DeepSeek

DeepSeek's pinned default [orchestrator](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md) and [dispatch](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/dispatch.md) prompts assign intake/routing to the main Session and technical implementation planning to Executors. Its [admission](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/admission.md) path also evaluates proactive candidates.

This Codex workflow adds project-level decomposition to the coordinating Router's responsibility. Model selection must account for that technical judgment and coupling analysis. The model implications are design inferences, assessed against actual workloads.

The reference to DeepSeek identifies an architectural influence. This repository supplies its own Codex-facing components, with provenance in [NOTICE](../NOTICE).

## Components shipped here

| Component | Implementation | Responsibility |
| --- | --- | --- |
| Role workflow | [SKILL.md](../skills/codex-superagent/SKILL.md) | Task decomposition, ownership, authorization, evidence and pause rules |
| Durable runtime | [task_runtime.py](../skills/codex-superagent/scripts/task_runtime.py) | Bindings, capacity, generations, subscriptions, event identity and dispatch state |
| Native-tool audit | [task_native_delivery.py](../skills/codex-superagent/scripts/task_native_delivery.py) | Intent, native tool outcome and recipient consumption |
| Plan continuation | [project_continuation.py](../skills/codex-superagent/scripts/project_continuation.py) | Existing plan/ledger dependencies, eligible work and evidence-review requests |
| Optional RPC host | [task_runtime_host.py](../skills/codex-superagent/scripts/task_runtime_host.py) | Bounded delivery through an existing verified compatible control interface |
| Local dashboard | [development-status](../tools/development-status/README.md) | Recorded team, task, public progress and plan associations |

The SQLite store and RPC transport retain their Agent Relay module names. The evidence verifier has a local bounded JSON reader. Guides and implementation remain focused on the current coordination workflow.

## Persistence and control

Routing state stores identity, bindings, generations, stable events, delivery intent, pauses and receipts. The original project records hold implementation and acceptance facts. Technical Owners maintain current evidence, and the Router records project-level decisions there.

The project configures a Worker cap. Unfinished waiting, paused and repair tasks continue to occupy capacity. Completed released history is outside the count. Independent-chat creation, internal subagents and communication follow their own current authorization and host limits.

A human pause blocks further dispatch while in-flight work reaches its safe save/stop boundary. Team and independent task resumes are verified separately. Existing teams and single-chat projects adopt changes through their current authorized handoff.

## Verification

Verify each of these outcomes with its own evidence.

1. The Router used current technical facts to choose useful task boundaries.
2. Local runtime checks handled bindings, pauses, unknown sends and event identity correctly.
3. The real host delivered an authorized event and the intended recipient consumed it.
4. The technical Owner delivered the required outcome and applicable acceptance.

Offline tests cover local runtime behavior. Decomposition quality and development efficiency are assessed from real project outcomes. Native-tool delivery records separate calls, and each host integration needs its own verification. Role instructions and local checks describe their scope of enforcement.

The optional RPC path depends on a compatible existing control interface. Model changes follow explicit authorization and supported tool parameters. Device, save, publication and account boundaries remain governed by the project.

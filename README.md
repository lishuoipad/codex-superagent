# Codex SuperAgent

[简体中文](README.zh-CN.md) · [Install](#get-started) · [Architecture](docs/architecture.md) · [Runtime guide](skills/codex-superagent/references/runtime.md) · [MIT](LICENSE)

Codex SuperAgent combines project-level task decomposition, complete-task ownership, durable handoffs and a local dashboard in a reusable Codex skill. **The Router/coordinating chat owns how the project is divided and which work can run independently. Each Worker owns the complete technical outcome of its assigned task.**

## How this differs from DeepSeek's original SuperAgent

The comparison uses the public research prototype's pinned default prompts and execution model.

| Dimension | DeepSeek public prototype | This Codex workflow |
| --- | --- | --- |
| Work intake | Authorized requests and enabled-rule candidates from sources such as GitHub and Feishu become Tickets | A user supplies a project goal or remaining plan, and the coordinating chat organizes the work |
| Main coordinator | The main Session assesses requests, routes Tickets and relays results. Default prompts assign implementation planning to Executors | The Router also owns project-level technical decomposition, coupling analysis, dependency order, write ownership and delivery boundaries |
| Execution | An Executor investigates and implements a Ticket, using internal subagents where appropriate | A Worker investigates, plans, implements, integrates, validates and repairs its complete task within authorization |
| Host | DeepSeek Harness, source subscriptions and its prototype runtime | Available Codex chat/subagent tools, workflow instructions and local durable-routing components |

DeepSeek's default dispatch prompt assigns technical implementation planning to Executors while its main Session handles assessment and routing. Proactive admission also involves authorization, intent and scope judgments. See the pinned [orchestrator prompt](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md), [dispatch rules](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/dispatch.md) and [admission instructions](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/admission.md).

**Model selection follows the responsibility.** Routing already bounded requests carries less project-level technical planning. This project's Router must understand module relationships and shared state well enough to evaluate decomposition and coordination cost. That is a design inference from the role, with model suitability and productivity requiring workload-specific verification.

## Project decomposition and implementation planning

- **The Router decides how to divide the project.** For example, whether mail and inventory screens are independent tasks, who owns their shared session state, which prerequisites come first, and who batches device acceptance.
- **The Worker decides how to implement its task.** For a mail screen, this includes the code to change, state and interface design, error handling and the checks needed to establish completion.

The Router uses real requirements, entry points and current evidence. It can read relevant code or request a bounded investigation from a Worker before deciding. Ordinary implementation choices and technical acceptance stay with the task Owner.

## Choose boundaries before concurrency

| Verified condition | Organization |
| --- | --- |
| Changes depend on the same mutable state, shared interface or continuous save procedure | Keep them with one Owner, or finish the shared interface before downstream work |
| Screens have independent implementation inputs and write scopes but share a phone for acceptance | Deliver independent work packages and batch compatible device cases with one field executor |
| Actual callers or the remaining gap are unclear | Investigate the bounded uncertainty, then let the Router decide the task boundary |
| A work package is delivered but overall acceptance has a dependency | Retain the unverified scope, Owner and release event until the agreed completion conditions are met |

Match concurrency to genuinely ready independent work. Use existing delivery, waiting, handoff and repair records to refine the split. Reuse passing evidence when its relevant inputs and conditions still match.

## Responsibilities and flow

~~~mermaid
flowchart LR
    U[User goal and existing plan] --> R[Router checks facts, decomposes work and coordinates dependencies]
    R --> A[Worker A complete task]
    R --> B[Worker B complete task]
    E[Known-task follow-up] --> A
    A --> D[Evidence, delivery and remaining dependencies]
    B --> D
    D --> R
    R --> U
~~~

The Router preserves the overall goal, decides project-level task boundaries and resolves cross-task conflicts. Workers own in-task planning, implementation, assigned shared wiring, affected validation, delivery and repairs. One shared file has one technical writer. Devices, accounts and saves follow the project's existing exclusive-access mechanisms.

Unfinished waiting, paused, approval and repair tasks count against the project Worker cap. New facts changing the split return to the Router, while ordinary known-task follow-ups stay with their Owner. An explicitly chosen single-chat workflow follows its current project agreement.

## Get started

Ask Codex's skill installer to install this directory.

~~~text
Use $skill-installer to install:
https://github.com/lishuoipad/codex-superagent/tree/main/skills/codex-superagent
~~~

Start with a concrete project scope and its existing plan/evidence.

~~~text
Use $codex-superagent to review this project's remaining development plan.
The coordinating chat owns project-level decomposition and parallel feasibility.
Check shared state, write scope, dependencies and independently useful outcomes.
Reuse existing authorized participants, test entry points and valid evidence.
Preserve feature scope, acceptance criteria and human pauses.
~~~

Creating independent chats requires an explicit request, and cross-chat messages require authorization covering the participants and purpose. Size a new team from ready work and actual host capacity. Existing teams follow their current handoff contract. Consult the [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills) for supported locations, and copy the complete `skills/codex-superagent` directory for manual installation.

## Implementation and host integration

| Capability | Implementation |
| --- | --- |
| Project-level decomposition and coupling analysis | Skill-guided technical judgment by the coordinating chat |
| Bindings, capacity, pauses and event deduplication | Local SQLite runtime with offline tests |
| Native-chat delivery | Adapter records intent, outcome and consumption. An authorized caller invokes the actual chat tool |
| Plan continuation | Read-only selector checks existing plan, dependencies and evidence. The Router evaluates boundaries and dispatch |
| Titles and storage closeout | Native title readback and project-required artifact disposition in existing handoff records |
| Local dashboard | Displays recorded state, public progress and plan associations |

Verify routing-state checks and decomposition quality separately. Native continuation depends on supported host interfaces and real delivery verification, with evidence for each separate tool call. Optional RPC delivery uses an existing verified control interface. See the [runtime guide](skills/codex-superagent/references/runtime.md) for integration, pauses, ambiguous sends and stopping authorized follow-ups.

## Try the offline core

Python 3.10+ and the standard library run the routing example and tests.

~~~bash
git clone https://github.com/lishuoipad/codex-superagent.git
cd codex-superagent
python -B examples/offline_demo.py
python -B -m unittest discover -s tests -v
~~~

The example uses fictional identities, temporary state and a paused development gate. Offline tests cover runtime behavior. Assess task-boundary quality and sustained development efficiency through real delivery, waiting and repair records.

## Repository and provenance

- [Installable skill](skills/codex-superagent/SKILL.md) with focused workflow, runtime and evidence guides.
- [Optional Windows dashboard](tools/development-status/README.md), offline examples and tests.
- [Architecture](docs/architecture.md), [provenance](NOTICE) and [MIT license](LICENSE).

The implementation grew from [Agent Relay](https://github.com/lishuoipad/agent-relay). Design references include DeepSeek's [domain model](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md) and [execution model](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md). This is an independently maintained community project. Contributions should provide a reproducible coordination problem, necessary evidence and affected checks, using fictional identities in public examples.

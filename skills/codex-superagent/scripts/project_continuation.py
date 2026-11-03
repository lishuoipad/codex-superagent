"""Read-only, evidence-aware continuation of an existing project plan.

No task, chat, Goal, development gate, or acceptance record is changed here.
Technical owners put small readiness declarations in the ORIGINAL ledger under
continuation.readyNextTasks. Router chooses routing; it does not redo acceptance.
A reported state or a verified file hash is not an independent technical audit.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
import sys

MAX_JSON_BYTES = 8 * 1024 * 1024
DEVELOPED = {"integrated_pending_acceptance", "implemented", "complete", "completed"}
OMITTED = {"excluded", "not_applicable"}
OMITTED_ACCEPTANCE = {"excluded", "not_applicable", "not_applicable_for_exact_excluded_scope"}
MODES = {"development", "acceptance", "review"}


def digest(value):
    """Canonical JSON hash. inspect returns this for each plan unit."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("expected an existing absolute JSON path <= 8 MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def verify_reference(ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return False
    try:
        path = Path(ref["path"])
        expected = ref.get("sha256")
        return (path.is_absolute() and path.is_file()
                and path.stat().st_size <= MAX_JSON_BYTES
                and isinstance(expected, str) and len(expected) == 64
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    except OSError:
        return False


def _dependencies(item, key):
    raw = item.get(key, item.get("dependsOn", []) if key == "developmentDependsOn" else [])
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x for x in raw):
        raise ValueError("dependency IDs must be nonempty strings in a list")
    return list(dict.fromkeys(raw))


def _index_plan(plan):
    indexed, order = {}, []
    def visit(item, parent=None):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("plan units need a unique nonempty id")
        key = item["id"]
        if key in indexed:
            raise ValueError("duplicate plan unit id: " + key)
        phase = item.get("phase", indexed[parent]["phase"] if parent else None)
        if not isinstance(phase, str) or not phase:
            raise ValueError("plan unit phase missing: " + key)
        inherited = indexed[parent]["developmentDependsOn"] if parent else []
        indexed[key] = {"item": item, "parentId": parent, "phase": phase,
                        "planUnitSha256": digest(item), "children": [],
                        "developmentDependsOn": list(dict.fromkeys(inherited + _dependencies(item, "developmentDependsOn"))),
                        "acceptanceDependsOn": _dependencies(item, "acceptanceDependsOn")}
        order.append(key)
        children = item.get("subtasks") or []
        if not isinstance(children, list):
            raise ValueError("subtasks must be a list")
        for child in children:
            visit(child, key)
            indexed[key]["children"].append(child["id"])
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("existing nonempty plan.tasks required")
    for task in tasks:
        visit(task)
    return indexed, order


def _state(unit):
    item = unit["item"]
    state = item.get("progressState")
    if not isinstance(state, dict):
        state = {}
    implementation = state.get("implementation", "unknown")
    acceptance = state.get("acceptance", "unknown")
    omitted = implementation in OMITTED and acceptance in OMITTED_ACCEPTANCE and bool(state.get("basis"))
    # Legacy status is deliberately displayed but cannot promote these values.
    return {"implementation": implementation, "acceptance": acceptance,
            "omitted": omitted, "legacyStatus": item.get("status"),
            "basis": str(state.get("basis", ""))[:500]}


def inspect_project(plan, ledger, team, runtime, scope=None, verify_ref=verify_reference):
    """Pure evaluation apart from the supplied bounded reference verifier.

    Scope is supplied by the authorized caller, never inferred from idle chats.
    Runtime must be a fresh TaskRuntime.status() read; this function is not a
    send permit. Host must recheck native state and the latest human stop.
    """
    units, order = _index_plan(plan)
    selected = scope if scope is not None else plan.get("workObjective", {}).get("scope", [])
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) or not x for x in selected):
        raise ValueError("explicit nonempty phase scope required")
    selected = list(dict.fromkeys(selected))
    unknown_phases = sorted(set(selected) - {u["phase"] for u in units.values()})
    if unknown_phases:
        raise ValueError("scope phases missing from plan: " + ",".join(unknown_phases))
    continuation = ledger.get("continuation", {})
    if not isinstance(continuation, dict) or continuation.get("schema", 1) != 1:
        raise ValueError("unsupported ledger.continuation schema")
    ready = continuation.get("readyNextTasks", [])
    if not isinstance(ready, list):
        raise ValueError("continuation.readyNextTasks must be a list")
    workers = runtime.get("workers", [])
    if not isinstance(workers, list):
        raise ValueError("runtime workers must be a list")
    live = [w for w in workers if w.get("state") != "completed"]
    known_owners = {w.get("threadId") for w in workers}
    technical_owner = team.get("technicalTaskOwner") or team.get("businessRecordWriter")
    if technical_owner:
        known_owners.add(technical_owner)
    known_owners.discard(None)
    router = team.get("routingOwner") or runtime.get("router", {}).get("threadId")
    known_owners.discard(router)
    paused_ids = continuation.get("pausedTaskIds", [])
    if not isinstance(paused_ids, list) or any(not isinstance(x, str) for x in paused_ids):
        raise ValueError("pausedTaskIds must be a list of task IDs")
    paused_ids = set(paused_ids)
    unknown_paused = sorted(paused_ids - set(units))
    if unknown_paused:
        raise ValueError("pausedTaskIds must reference exact plan IDs; unknown: " + ",".join(unknown_paused))
    gate = runtime.get("developmentEnabled") is True and team.get("development", {}).get("enabled") is True
    states = {k: _state(v) for k, v in units.items()}
    scoped = [key for key in order if units[key]["phase"] in selected]
    mandatory = [key for key in scoped if not states[key]["omitted"]]
    proof_cache = {}
    def proof(ref):
        cache_key = digest(ref)
        if cache_key not in proof_cache:
            proof_cache[cache_key] = bool(verify_ref(ref))
        return proof_cache[cache_key]
    def explicitly_paused(key):
        parent = units[key]["parentId"]
        return key in paused_ids or bool(parent and explicitly_paused(parent))
    def developed(key, visiting=None):
        visiting = set() if visiting is None else visiting
        if key in visiting:
            return False
        if states[key]["omitted"]:
            return True
        if states[key]["implementation"] not in DEVELOPED or not states[key]["basis"]:
            return False
        return all(developed(child, visiting | {key}) for child in units[key]["children"])
    def accepted(key):
        if states[key]["omitted"]:
            return True
        return (developed(key) and states[key]["acceptance"] == "passed"
                and all(accepted(child) for child in units[key]["children"]))
    def task_proof(key):
        item = units[key]["item"]
        refs = item.get("evidence", [])
        if not isinstance(refs, list):
            refs = []
        extra = item.get("progressState", {}).get("evidenceRef") if isinstance(item.get("progressState"), dict) else None
        return any(proof(ref) for ref in refs if isinstance(ref, dict)) or bool(extra and proof(extra))
    # Detect real dependency cycles without allowing an acceptance edge to block development.
    def cycle_from(start, kind):
        field = kind + "DependsOn"
        def walk(key, stack, done):
            if key in stack:
                return True
            if key in done or key not in units:
                return False
            done.add(key)
            return any(walk(dep, stack | {key}, done) for dep in units[key][field])
        return walk(start, set(), set())
    ready_candidates, rejected = [], []
    declaration_counts = Counter((e.get("taskId"), e.get("mode")) for e in ready if isinstance(e, dict)
                                and isinstance(e.get("taskId"), str) and isinstance(e.get("mode"), str))
    modes_by_task = {}
    for declaration in ready:
        if isinstance(declaration, dict) and isinstance(declaration.get("taskId"), str) and isinstance(declaration.get("mode"), str):
            modes_by_task.setdefault(declaration["taskId"], set()).add(declaration["mode"])
    binding_by_task = {w.get("taskBindingId"): w for w in live}
    for index, entry in enumerate(ready):
        if not isinstance(entry, dict):
            rejected.append({"index": index, "reason": "invalid_readiness_declaration"}); continue
        key, mode = entry.get("taskId"), entry.get("mode")
        problems = []
        if not isinstance(key, str) or key not in units:
            rejected.append({"index": index, "taskId": key, "reason": "unknown_plan_task"}); continue
        if not isinstance(key, str) or not isinstance(mode, str):
            rejected.append({"index": index, "reason": "invalid_readiness_identity"}); continue
        if len(modes_by_task.get(key, set())) > 1:
            rejected.append({"index": index, "taskId": key, "reason": "conflicting_modes_for_task"}); continue
        if declaration_counts[(key, mode)] > 1:
            rejected.append({"index": index, "taskId": key, "reason": "duplicate_readiness_declaration"}); continue
        if mode not in MODES: problems.append("unsupported_mode")
        if units[key]["phase"] not in selected: problems.append("outside_requested_scope")
        if entry.get("planUnitSha256") != units[key]["planUnitSha256"]: problems.append("stale_plan_unit")
        if not isinstance(entry.get("reviewedBy"), str) or entry.get("reviewedBy") not in known_owners: problems.append("current_technical_owner_required")
        if not proof(entry.get("evidenceRef")): problems.append("missing_or_changed_owner_evidence")
        if explicitly_paused(key): problems.append("independent_task_paused")
        if states[key]["omitted"]: problems.append("excluded_or_not_applicable_scope")
        if accepted(key): problems.append("task_already_reports_accepted")
        if entry.get("actionClass", "development") not in {"development", "acceptance", "review"}:
            problems.append("external_action_requires_separate_authority")
        dep_proofs = entry.get("dependencyEvidence", [])
        if not isinstance(dep_proofs, list):
            dep_proofs = []; problems.append("invalid_dependency_evidence")
        def dependency_proven(dep, kind):
            if dep not in units: return False
            return any(isinstance(d, dict) and d.get("taskId") == dep and d.get("kind") == kind
                       and d.get("planUnitSha256") == units[dep]["planUnitSha256"]
                       and proof(d.get("evidenceRef")) for d in dep_proofs)
        if mode in {"development", "acceptance"}:
            for kind in (["development"] if mode == "development" else ["development", "acceptance"]):
                if cycle_from(key, kind): problems.append(kind + "_dependency_cycle")
                for dep in units[key][kind + "DependsOn"]:
                    if dep not in units:
                        problems.append("unknown_" + kind + "_dependency:" + dep); continue
                    if explicitly_paused(dep):
                        problems.append("paused_dependency:" + dep); continue
                    state_ready = developed(dep) if kind == "development" else accepted(dep)
                    if not dependency_proven(dep, kind) and not (state_ready and task_proof(dep)):
                        problems.append("unverified_" + kind + "_dependency:" + dep)
            if mode == "acceptance" and not developed(key):
                problems.append("implementation_not_reported_ready_for_acceptance")
        binding = entry.get("taskBindingId")
        known_binding = binding_by_task.get(key)
        if known_binding:
            if binding is not None and binding != key:
                problems.append("known_live_binding_conflict")
            binding = key
        if binding is not None and not isinstance(binding, str):
            problems.append("invalid_task_binding_id"); binding = None
        owner = binding_by_task.get(binding) if binding else None
        if binding and owner is None: problems.append("specified_binding_is_not_live")
        if owner and owner.get("state") == "paused": problems.append("bound_worker_paused")
        if owner and entry.get("ownerThreadId", owner.get("threadId")) != owner.get("threadId"):
            problems.append("binding_owner_conflict")
        priority = entry.get("priority", 1000)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not math.isfinite(priority):
            problems.append("invalid_priority"); priority = 1000
        if problems:
            rejected.append({"index": index, "taskId": key, "mode": mode, "reasons": problems}); continue
        ready_candidates.append({"taskId": key, "mode": mode, "title": units[key]["item"].get("title", key),
                                 "planUnitSha256": units[key]["planUnitSha256"], "priority": priority,
                                 "evidenceRef": entry["evidenceRef"], "reviewedBy": entry["reviewedBy"],
                                 "taskBindingId": binding, "ownerThreadId": owner.get("threadId") if owner else None,
                                 "route": "existing_owner" if owner else "router_assignment",
                                 "declarationIndex": index})
    ready_candidates.sort(key=lambda e: (e["priority"], order.index(e["taskId"]), e["declarationIndex"]))
    # A parent and one of its subtasks must not be assigned concurrently from one selection.
    top_ready = []
    for candidate in ready_candidates:
        ancestors, parent = set(), units[candidate["taskId"]]["parentId"]
        while parent:
            ancestors.add(parent); parent = units[parent]["parentId"]
        if not any(x["taskId"] in ancestors or candidate["taskId"] in _ancestors(units, x["taskId"]) for x in top_ready):
            top_ready.append(candidate)
    remaining = [key for key in scoped if not accepted(key)]
    owner_review = [{"taskId": key, "title": units[key]["item"].get("title", key),
                     "planUnitSha256": units[key]["planUnitSha256"],
                     "reason": "current_owner_readiness_or_acceptance_evidence_needed",
                     "ownerThreadId": technical_owner}
                    for key in remaining if not explicitly_paused(key) and not states[key]["omitted"]
                    and not any(c["taskId"] == key for c in top_ready)]
    completion_review = continuation.get("completionReview", {})
    completion_ok = (isinstance(completion_review, dict)
        and completion_review.get("result") == "passed"
        and completion_review.get("planSha256") == digest(plan)
        and completion_review.get("scopePhases") == selected
        and completion_review.get("reviewedBy") == technical_owner and technical_owner is not None
        and completion_review.get("requiredTaskIds") == mandatory
        and completion_review.get("remainingRequiredItems") == []
        and completion_review.get("unresolvedDefects") == []
        and proof(completion_review.get("evidenceRef")))
    unfinished = [{"threadId": w.get("threadId"), "taskBindingId": w.get("taskBindingId"),
                   "state": w.get("state"), "workState": w.get("workState", w.get("state"))} for w in live]
    complete = not remaining and not any(explicitly_paused(k) for k in scoped) and not live and completion_ok
    limit = runtime.get("maxInUseWorkers")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("explicit runtime Worker cap required")
    report = {"schema": 1, "readOnly": True, "developmentEnabled": gate, "scopePhases": selected,
              "planSha256": digest(plan), "originalPlanScope": plan.get("workObjective", {}).get("scope", []),
              "scopeAuthority": "caller must verify explicit human scope and resume authority",
              "inUseWorkers": len(live), "maxInUseWorkers": limit,
              "availableExistingWorkers": [w["threadId"] for w in workers if w.get("state") == "completed"],
              "unfinishedBindings": unfinished, "readyCandidates": top_ready, "rejectedDeclarations": rejected,
              "ownerReviewRequired": owner_review,
              "pausedTaskIds": [key for key in scoped if explicitly_paused(key)],
              "units": [{"taskId": key, "parentId": units[key]["parentId"], "phase": units[key]["phase"],
                         "planUnitSha256": units[key]["planUnitSha256"], **states[key]} for key in scoped],
              "completion": {"complete": complete, "remainingTaskIds": remaining,
                             "requiredTaskIds": mandatory, "ownerReviewVerified": completion_ok,
                             "unfinishedBindingsRemain": bool(live),
                             "meaning": "reported plan acceptance plus current owner review; not an independent audit"}}
    report["nextAction"] = next_action(report, technical_owner)
    return report


def _ancestors(units, key):
    result, parent = set(), units[key]["parentId"]
    while parent:
        result.add(parent); parent = units[parent]["parentId"]
    return result


def next_action(report, technical_owner=None):
    if not report["developmentEnabled"]:
        return {"kind": "paused", "reason": "latest_team_or_runtime_gate_is_not_enabled"}
    if report["completion"]["complete"]:
        return {"kind": "complete", "reason": "all_required_acceptance_and_owner_review_recorded"}
    owner_paused = any(w["threadId"] == technical_owner and w["state"] == "paused" for w in report["unfinishedBindings"])
    if not report["completion"]["remainingTaskIds"]:
        if report["pausedTaskIds"] or owner_paused:
            return {"kind": "waiting", "reason": "independent_pause_still_applies_at_closeout"}
        if report["unfinishedBindings"]:
            return {"kind": "request_owner_closeout", "ownerThreadId": technical_owner,
                    "unfinishedBindings": report["unfinishedBindings"],
                    "reason": "reported_acceptance_is_not_task_release; verify_pending_work_and_completion_review"}
        if not report["completion"]["ownerReviewVerified"]:
            return {"kind": "request_completion_review", "ownerThreadId": technical_owner,
                    "reason": "all_plan_units_report_accepted_but_current_completion_review_is_missing_or_invalid"}
    for candidate in report["readyCandidates"]:
        if candidate["route"] == "existing_owner":
            return {"kind": "continue_owner", "candidate": candidate}
        if report["availableExistingWorkers"] and report["inUseWorkers"] < report["maxInUseWorkers"]:
            return {"kind": "route_ready", "candidate": candidate,
                    "eligibleExistingWorkerIds": report["availableExistingWorkers"]}
    if report["readyCandidates"]:
        return {"kind": "waiting_for_existing_capacity", "reason": "unfinished_waiting_tasks_still_count; no_automatic_chat_creation"}
    if report["ownerReviewRequired"] and technical_owner:
        if owner_paused:
            return {"kind": "waiting", "reason": "technical_owner_independently_paused"}
        return {"kind": "request_owner_review", "ownerThreadId": technical_owner,
                "taskId": report["ownerReviewRequired"][0]["taskId"],
                "planUnitSha256": report["ownerReviewRequired"][0]["planUnitSha256"],
                "reason": "prepare_next_work_in_original_ledger; do_not_repeat_already_running_work"}
    return {"kind": "waiting", "reason": "no_verified_ready_task_or_completion; inspect_explicit_dependencies"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "next"])
    for name in ("plan", "ledger", "team", "runtime-config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--scope", help="comma-separated explicitly authorized phases; default existing objective")
    args = parser.parse_args()
    from task_runtime import TaskRuntime
    plan, ledger, team = (read_json(getattr(args, name)) for name in ("plan", "ledger", "team"))
    runtime = TaskRuntime(read_json(args.runtime_config)).status()
    scope = args.scope.split(",") if args.scope else None
    result = inspect_project(plan, ledger, team, runtime, scope)
    print(json.dumps(result if args.command == "inspect" else result["nextAction"], ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc), "readOnly": True}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)

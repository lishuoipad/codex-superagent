"""Finite supervision reducer and tool-host bridge. No implicit model calls.

The host supplies fresh bounded public state; project adapters read selected
fields only. This module never opens another Codex backend or dispatches work.
Only a reserved notification proposal may be handed to an authorized host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import time

from supervision_store import SupervisionStore
from stalled_turn import active_review, running_tools
from failure_diagnostics import classify_error, failure_review

TERMINAL = {"completed", "closed", "superseded"}
RECORD_LIMIT = 2 * 1024 * 1024
SAMPLE_KEYS = {"threadId", "hostId", "runtimeInstanceId", "sourceInstanceId", "cursor", "observedAt",
               "status", "activeFlags", "turnId", "turnStatus", "capacityFailure",
               "willRetry", "readError", "paused", "failureKind", "failurePhase", "completedCommandId", "runningTools"}


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, maximum=RECORD_LIMIT):
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("absolute evidence path required")
    before = p.stat()
    if before.st_size > maximum:
        raise ValueError("evidence exceeds bounded read limit")
    with p.open("rb") as stream:
        raw = stream.read(maximum + 1)
    after = p.stat()
    if len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("evidence changed during read")
    return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def pointer(value, path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("JSON pointer required")
    for part in path.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def project_record(mapping):
    """No fuzzy prose interpretation, directory scans, or project writes."""
    if not mapping:
        return {"known": False, "reason": "no_project_adapter"}
    try:
        source, file_sha = read_json(mapping["path"])
        selected = pointer(source, mapping["pointer"])
        if not isinstance(selected, dict):
            raise ValueError("structured task record required")
        allowed = {"taskBindingId", "taskState", "assignmentState", "decisionOwner",
                   "reason", "nextReviewAt", "dependencyVersion", "freezeVersion",
                   "batchVersion", "defectVersion", "decisionRef", "dispatchRef",
                   "operationId", "updatedAt", "evidence", "supersedes",
                   "deliveryRef", "reviewProgress"}
        record = {k: v for k, v in selected.items() if k in allowed}
        if not record.get("taskBindingId") or not record.get("taskState"):
            raise ValueError("task identity/state missing")
        if len(json.dumps(record)) > 16384:
            raise ValueError("selected project record too large")
        return {"known": True, "record": record, "version": digest(record),
                "source": {"path": mapping["path"], "pointer": mapping["pointer"],
                           "sha256": file_sha}}
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {"known": False, "reason": "project_record_unavailable"}


def delivery_version(record):
    """A delivery survives waiting turns and owner progress edits.

    Legacy unversioned records are one delivery per task binding. A replacement
    must supply deliveryRef; never infer a new delivery from a chat turn.
    """
    for key in ("deliveryRef", "evidence"):
        ref = record.get(key)
        sha = ref.get("sha256") if isinstance(ref, dict) else None
        if (isinstance(sha, str) and len(sha) == 64
                and all(c in "0123456789abcdefABCDEF" for c in sha)):
            return sha.lower()
    return digest(["unversioned_delivery", record["taskBindingId"]])


def review_progress(record):
    """Optional owner-authored context, never an acceptance/recovery signal."""
    progress = record.get("reviewProgress")
    if not isinstance(progress, dict):
        return None
    sha = progress.get("deliverySha256")
    if not isinstance(sha, str) or sha.lower() != delivery_version(record):
        return None
    result = {"deliverySha256": sha.lower(), "truncated": False}
    for key in ("summary", "updatedAt"):
        value = progress.get(key)
        if isinstance(value, str):
            result[key] = value[:512]
            result["truncated"] |= len(value) > 512
    for key in ("completedChecks", "remainingChecks", "waitingOn"):
        value = progress.get(key)
        if isinstance(value, list):
            result[key] = [v[:512] for v in value[:20] if isinstance(v, str)]
            result["truncated"] |= len(value) > 20 or any(
                isinstance(v, str) and len(v) > 512 for v in value[:20])
    return result


def public_sample(poll, observed_at, host_id="local", turn_read=None):
    """Normalize a wait_threads poll without retaining text or tool payloads."""
    thread = poll["thread"]
    turn = poll.get("latestTurn") or {}
    status = thread.get("status", {})
    error = turn.get("error") or {}
    failure = classify_error(error)
    cursor = str(poll.get("cursor") or "")
    marker = poll.get("latestToolMarker") or {}
    command_id = None
    if (marker.get("turnId") == turn.get("id") and marker.get("type") == "commandExecution"
            and marker.get("status") == "completed" and marker.get("id") == poll.get("latestToolMarkerId")):
        command_id = marker.get("id")
    host_id = thread.get("hostId", host_id)
    return {"threadId": thread["id"], "hostId": host_id,
            "completedCommandId": command_id,
            "runningTools": running_tools(turn_read, thread["id"], host_id, turn.get("id"), command_id),
            # A poll cursor identifies the tool observation stream, not the
            # native backend process. Never fabricate native recovery identity.
            "sourceInstanceId": cursor.rsplit(":", 1)[0] if cursor else "",
            "cursor": cursor, "observedAt": observed_at,
            "status": status.get("type", "unknown"),
            "activeFlags": status.get("activeFlags", []),
            "turnId": turn.get("id"), "turnStatus": turn.get("status"),
            "capacityFailure": failure["kind"] == "capacity",
            "failureKind": failure["kind"], "failurePhase": failure["phase"],
            "willRetry": any(source.get("willRetry") is True for source in (error, turn, poll))}


def evidence_ref(value):
    if not isinstance(value, dict) or not value.get("path") or not value.get("sha256"):
        return False
    try:
        _, actual = read_json(value["path"])
        return actual == value["sha256"]
    except (OSError, ValueError, TypeError):
        return False


def sample_time(sample):
    value = sample.get("observedAt")
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value)) else None


def observation_order(sample, old, now, valid_identity=True):
    """Keep source clocks across failures/restarts; never sort opaque cursors."""
    previous = old.get("sample", {})
    watermark = old.get("observationWatermark") or {
        "observedAt": sample_time(previous), "sources": {}}
    watermark = {**watermark, "sources": dict(watermark.get("sources", {}))}
    observed, prior = sample_time(sample), watermark.get("observedAt")
    if sample.get("readError") or observed is None or observed > now or not valid_identity:
        # A failed read invalidates earlier successful observations too.
        watermark["observedAt"] = max(now, prior) if prior is not None else now
        return None, watermark
    if prior is not None and observed < prior:
        return "observation_time_regressed", watermark
    source = sample.get("sourceInstanceId") or sample.get("runtimeInstanceId")
    cursor = sample.get("cursor")
    if isinstance(source, str) and source and isinstance(cursor, str) and cursor:
        prefix, separator, suffix = cursor.rpartition(":")
        position = int(suffix) if separator and suffix.isdecimal() and len(suffix) <= 20 else None
        saved = watermark["sources"].get(source)
        # Derive an existing cursor on the first observation after an upgrade.
        if saved is None and source == (previous.get("sourceInstanceId") or previous.get("runtimeInstanceId")):
            old_prefix, old_separator, old_suffix = str(previous.get("cursor", "")).rpartition(":")
            if old_separator and old_suffix.isdecimal() and len(old_suffix) <= 20:
                saved = {"prefix": old_prefix, "position": int(old_suffix)}
        if saved and (position is None or prefix != saved["prefix"] or position < saved["position"]):
            return "observation_cursor_regressed", watermark
        if position is not None:
            if source not in watermark["sources"] and len(watermark["sources"]) >= 32:
                return "observation_source_capacity", watermark
            watermark["sources"][source] = {"prefix": prefix, "position": position}
    watermark["observedAt"] = observed
    return None, watermark


class Supervisor:
    def __init__(self, store, run_id, contract):
        registered = store.run_status(run_id)["contract"]
        if digest(registered) != digest(contract):
            raise ValueError("contract drift; no API override of the registered run")
        # The registry read provides a detached copy. Never retain the caller's
        # mutable contract as the authorization used by the tool-host API.
        self.store, self.run_id, self.contract = store, run_id, registered
        self.targets = {t["threadId"]: t for t in registered["targets"]}
        self.owner = registered["owner"]
        if isinstance(self.owner, dict):
            self.owner = self.owner["threadId"]

    def _load(self):
        current = self.store.get_record(self.run_id, "supervision")
        value = current.get("value") or {"targets": {}, "events": {}, "notifications": {}}
        return current["revision"], value

    def _save(self, revision, state):
        self.store.put_record(self.run_id, "supervision", state, revision)

    def observe(self, samples, now, records=None):
        revision, state = self._load()
        by_id = {s.get("threadId"): s for s in samples}
        changes = []
        for tid, target in self.targets.items():
            old = state["targets"].get(tid, {})
            sample = {k: v for k, v in by_id.get(tid, {}).items() if k in SAMPLE_KEYS}
            source = sample.get("sourceInstanceId") or sample.get("runtimeInstanceId")
            valid_identity = (sample.get("hostId") == self.contract.get("hostId", "local")
                              and isinstance(source, str) and bool(source))
            rejected, watermark = observation_order(sample, old, now, valid_identity)
            if rejected:
                # Do not let an old response replace a newer pause or refresh its
                # freshness. The existing sample must still pass prepare's clock.
                state["targets"][tid] = {**old, "observationRejected": rejected}
                continue
            project = ((records or {}).get(tid) if records is not None
                       else project_record(target.get("projectRecord")))
            project = project or {"known": False, "reason": "missing_project_record"}
            result = self.classify(target, sample, project, old, now)
            record = project.get("record", {}) if project.get("known") else {}
            if record.get("taskBindingId") == target["taskBindingId"]:
                progress = review_progress(record)
                if progress:
                    result["reviewProgress"] = progress
            result["sample"] = sample
            result["project"] = project
            result["lastObservedAt"] = now
            result["observationWatermark"] = watermark
            # Dependency edges are events, not a one-poll state. Keep a pending
            # edge attached to its exact decision version until it is consumed.
            if result["state"] == "standby_with_reason":
                for event in state["events"].values():
                    if event["threadId"] == tid and event["reason"] == "standby_dependency_changed":
                        if (event["sourceVersion"] == result.get("reviewVersion")
                                and event["stage"] not in {"closed", "action_verified"}):
                            result["reviewReason"] = "standby_dependency_changed"
                        elif event["sourceVersion"] != result.get("reviewVersion") and event["stage"] != "closed":
                            event.update(stage="closed", closedReason="decision_or_dependency_superseded",
                                         closedAt=now)
            for event in state["events"].values():
                if (event["threadId"] != tid or event["reason"] != "completed_command_without_followup"
                        or event["stage"] not in {"detected", "delivered"} or result["state"] == "unknown"):
                    continue
                prior_turn, _, prior_command = event["sourceVersion"].partition("/")
                current_turn, command = sample.get("turnId"), sample.get("completedCommandId")
                if current_turn and (current_turn != prior_turn or (command and command != prior_command)
                                     or sample.get("turnStatus") in {"completed", "interrupted", "failed"}
                                     or sample.get("runningTools") or sample.get("willRetry")):
                    event.update(stage="closed", closedReason="new_native_progress", closedAt=now)
            state["targets"][tid] = result
            # Safety and presentation are separate. The first failed read always
            # removes eligibility; the second failure only changes display.
            if result["state"] != old.get("state"):
                changes.append({"threadId": tid, "state": result["state"]})
            reason = result.get("reviewReason")
            if result["state"] in {"closed", "superseded"} or (
                    result["state"] == "completed" and not reason):
                for event in state["events"].values():
                    if event["threadId"] == tid and event["stage"] != "closed":
                        event.update(stage="closed", closedReason="task_terminal", closedAt=now)
            if reason:
                # Exclude observation time and unrelated source-file changes.
                event_id = digest([self.run_id, tid, target["taskBindingId"], reason,
                                   result.get("reviewVersion")])[:32]
                for previous in state["events"].values():
                    if (previous["threadId"] == tid and previous["eventId"] != event_id
                            and previous["stage"] != "closed"):
                        previous.update(stage="closed", closedReason="decision_or_dependency_superseded",
                                        closedAt=now)
                if event_id not in state["events"]:
                    if len(state["events"]) >= 128:
                        raise ValueError("run event capacity reached; stop and review")
                    state["events"][event_id] = {
                        "eventId": event_id, "threadId": tid,
                        "taskBindingId": target["taskBindingId"], "reason": reason,
                        "stage": "detected", "detectedAt": now,
                        "nextReviewAt": now + 300, "escalated": False,
                        "sourceVersion": result.get("reviewVersion"),
                    }
        self._save(revision, state)
        return {"targets": {k: {f: v.get(f) for f in (
            "state", "displayState", "readFailures", "recoveryEligible", "reviewReason", "reviewProgress")}
            for k, v in state["targets"].items()}, "changes": changes,
            "pending": [e["eventId"] for e in state["events"].values() if e["stage"] != "closed"]}

    def classify(self, target, sample, project, old, now):
        observed = sample.get("observedAt")
        fresh = (isinstance(observed, (int, float)) and not isinstance(observed, bool)
                 and math.isfinite(observed) and 0 <= now - observed <= 120
                 and sample.get("hostId") == self.contract.get("hostId", "local")
                 and sample.get("threadId") == target["threadId"]
                 and (sample.get("runtimeInstanceId") or sample.get("sourceInstanceId"))
                 and not sample.get("readError"))
        base = {"recoveryEligible": False, "readFailures": 0}
        if not fresh:
            count = old.get("readFailures", 0) + 1
            return {**base, "state": "unknown", "displayState": "unknown" if count >= 2 else "stale",
                    "readFailures": count}
        if sample.get("paused") or any(x in sample.get("activeFlags", []) for x in (
                "waitingOnApproval", "waitingOnUserInput")):
            return {**base, "state": "paused_or_needs_human"}
        if sample.get("status") not in {"idle", "active", "systemError"}:
            return {**base, "state": "unknown"}
        if not project.get("known"):
            # Missing project metadata requests a decision, never recovery.
            return {**base, "state": "unknown", "reviewReason": "project_record_missing",
                    "reviewVersion": target["taskBindingId"]}
        record = project["record"]
        if record.get("taskBindingId") != target["taskBindingId"]:
            return {**base, "state": "superseded"}
        task_state, assignment = record["taskState"], record.get("assignmentState")
        if task_state in {"closed", "superseded"}:
            return {**base, "state": task_state}
        if task_state == "completed" and assignment != "standby":
            return {**base, "state": "completed"}
        # The host may be active while the worker waits for acceptance. Keep
        # genuine failed-turn capacity classification as before; active alone
        # must not hide an already recorded delivery.
        failed_capacity = (sample.get("capacityFailure") and not sample.get("willRetry")
                           and sample.get("turnStatus") == "failed"
                           and sample.get("status") != "active")
        if task_state == "delivered_waiting_acceptance" and not failed_capacity:
            return {**base, "state": "delivery_needs_decision",
                    "reviewReason": "delivery_needs_decision",
                    "reviewVersion": delivery_version(record)}
        if sample.get("status") == "active" or sample.get("willRetry"):
            review = active_review(sample, target.get("stallCheck"), now) if assignment != "standby" else None
            return {**base, **(review or {"state": "active"})}
        if task_state == "completed" and assignment == "standby" or assignment == "standby":
            due = record.get("nextReviewAt")
            reason = None
            if not record.get("reason") or not record.get("decisionOwner") or not isinstance(due, (int, float)):
                reason = "standby_decision_incomplete"
            elif not math.isfinite(due) or due <= now:
                reason = "standby_review_due"
            previous = old.get("project", {}).get("record", {})
            dependencies = ["dependencyVersion", "freezeVersion", "batchVersion", "defectVersion"]
            if previous and any(previous.get(k) != record.get(k) for k in dependencies):
                reason = "standby_dependency_changed"
            return {**base, "state": "standby_with_reason", "reviewReason": reason,
                    "reviewVersion": digest({k: record.get(k) for k in dependencies + [
                        "nextReviewAt", "decisionRef", "reason"]})}
        if sample.get("capacityFailure") and not sample.get("willRetry") and sample.get("turnStatus") == "failed":
            return {**base, "state": "capacity_failure", "reviewReason": "capacity_needs_checked_recovery",
                    "reviewVersion": sample.get("turnId")}
        failed_review = failure_review(sample)
        if failed_review is not None:
            return {**base, **failed_review}
        reason = "idle_without_decision"
        return {**base, "state": reason, "reviewReason": reason,
                "reviewVersion": digest([sample.get("turnId"), project.get("version")])}

    def prepare_notification(self, now):
        revision, state = self._load()
        if any(n["status"] in {"reserved", "unknown"} for n in state["notifications"].values()):
            return {"allowed": False, "reason": "prior_notification_outcome_unknown"}
        pending = [e for e in state["events"].values() if e["stage"] == "detected"]
        if not pending:
            return {"allowed": False, "reason": "no_new_actionable_events"}
        window = self.contract.get("policy", {}).get("coalesceWindowSeconds", 60)
        if now < min(e["detectedAt"] for e in pending) + window:
            return {"allowed": False, "reason": "coalescing"}
        # Fresh observations are required even when a notification was queued.
        ids = []
        for event in pending:
            target = state["targets"].get(event["threadId"], {})
            observed = sample_time(target.get("sample", {}))
            if (observed is not None and 0 <= now - observed <= 120
                    and target.get("state") not in {"paused_or_needs_human", "closed", "superseded"}
                    and target.get("reviewReason") == event["reason"]
                    and target.get("reviewVersion") == event["sourceVersion"]):
                ids.append(event["eventId"])
        if not ids:
            return {"allowed": False, "reason": "no_fresh_actionable_events"}
        if self.contract.get("messageAuthority", {}).get("authorized") is not True:
            return {"allowed": False, "reason": "message_authority_missing"}
        notification_id = digest([self.run_id, self.owner, sorted(ids)])[:32]
        permit = self.store.reserve_notification(self.run_id, notification_id, self.owner, sorted(ids), now)
        if not permit["allowed"]:
            return permit
        state["notifications"][notification_id] = {
            "status": "reserved", "intentId": permit["intentId"], "events": ids,
            "reservedAt": now, "recipient": self.owner,
        }
        for eid in ids:
            state["events"][eid].update(stage="delivery_attempted", notificationId=notification_id)
        self._save(revision, state)
        summary = []
        for eid in ids:
            event = state["events"][eid]
            entry = {k: event[k] for k in ("eventId", "threadId", "reason")}
            progress = state["targets"][event["threadId"]].get("reviewProgress")
            if progress:
                entry["reviewProgress"] = progress
            if event["reason"] == "completed_command_without_followup":
                entry["stallEvidence"] = state["targets"][event["threadId"]].get("stallEvidence")
            summary.append(entry)
        return {**permit, "notificationId": notification_id, "recipient": self.owner,
                "events": summary, "validUntil": min(now + 60, self.contract["deadline"]),
                "message": self.message(notification_id, summary)}

    def message(self, notification_id, events):
        stall_events = [e for e in events if e["reason"] == "completed_command_without_followup"]
        events = [e for e in events if e["reason"] != "completed_command_without_followup"]
        advisory = []
        if stall_events:
            advisory.append("Review the following active turns. A command finished and a model request failed. "
                            "Check running work, retries, queued input and existing artifacts.")
            advisory.extend(e["threadId"] + " " + str((e.get("stallEvidence") or {}).get("commandId")) for e in stall_events)
        if not events:
            return "\n".join(advisory + ["Notification ID " + notification_id])
        lines = ["Review these changes against the current task record and its authorized scope.",
                 "Events: " + ", ".join(e["threadId"] + " " + e["reason"] for e in events),
                 "Check current ownership, pending work and dependencies before any dispatch.",
                 "Notification ID " + notification_id]
        for event in events:
            progress = event.get("reviewProgress")
            if progress:
                lines.append(event["threadId"] + " recorded review progress: " + json.dumps(progress, ensure_ascii=False))
        for field, label in (("decisionReceiptPath", "Receipt"), ("ownerInstructionsRef", "Current instructions")):
            if self.contract.get(field):
                lines.append(label + ": " + self.contract[field])
        return "\n".join(lines + advisory)

    def notification_result(self, notification_id, status, now, evidence=None):
        if status not in {"delivered", "not_sent", "unknown"}:
            raise ValueError("invalid notification result")
        revision, state = self._load()
        n = state["notifications"][notification_id]
        if n["status"] != "reserved":
            raise ValueError("notification outcome already recorded; explicit reconciliation required")
        self.store.complete_intent(self.run_id, n["intentId"], "notification",
                                   "accepted" if status == "delivered" else status, evidence)
        n.update(status=status, at=now)
        for eid in n["events"]:
            event = state["events"][eid]
            if event["stage"] == "delivery_attempted" and event.get("notificationId") == notification_id:
                event.update(stage="delivered" if status == "delivered" else status,
                             nextReviewAt=now + 300)
        self._save(revision, state)

    def owner_receipt(self, receipt, now):
        """Record owner decisions; require independently read evidence, no trust by wording."""
        revision, state = self._load()
        if receipt.get("runId") != self.run_id or receipt.get("owner") != self.owner:
            raise ValueError("receipt identity mismatch")
        stage = receipt.get("stage")
        if stage not in {"acknowledged", "decision_recorded", "action_verified", "closed"}:
            raise ValueError("invalid owner receipt stage")
        event = state["events"][receipt["eventId"]]
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("sha256"), str):
            raise ValueError("independently readable JSON evidence required")
        receipt_id = digest([self.run_id, event["eventId"], stage, evidence["sha256"]])
        # File location is not receipt identity. Record the semantic payload so
        # the same proof cannot silently authorize a different decision.
        payload = {key: receipt.get(key) for key in (
            "decision", "reason", "nextReviewAt", "newTaskBindingId", "dependencyVersion", "supersedesReceiptId")}
        for key in ("dispatchEvidence", "progressEvidence"):
            value = receipt.get(key)
            payload[key] = value.get("sha256") if isinstance(value, dict) else value
        payload_hash = digest(payload)
        receipts = event.get("receipts", {})
        if receipt_id in receipts:
            if receipts[receipt_id]["payloadHash"] != payload_hash:
                raise ValueError("receipt replay changed its decision payload")
            return {"eventId": event["eventId"], "stage": event["stage"],
                    "receiptId": receipt_id, "duplicate": True}
        if not evidence_ref(evidence):
            raise ValueError("independently readable JSON evidence required")
        if event["stage"] in {"closed", "unknown", "not_sent", "detected", "delivery_attempted"}:
            raise ValueError("receipt cannot skip delivery or reopen a terminal event")
        order = ["delivered", "acknowledged", "decision_recorded", "action_verified", "closed"]
        if order.index(stage) < order.index(event["stage"]) or order.index(stage) > order.index(event["stage"]) + 1:
            raise ValueError("receipt skipped or regressed a stage")
        same_stage = stage == event["stage"]
        if same_stage and (not event.get("lastReceiptId")
                           or receipt.get("supersedesReceiptId") != event["lastReceiptId"]):
            raise ValueError("same-stage update requires supersedesReceiptId")
        if len(receipts) >= 32:
            raise ValueError("event receipt capacity reached; explicit review required")
        if stage == "decision_recorded":
            if receipt.get("decision") not in {"dispatch", "wait"}:
                raise ValueError("explicit dispatch or wait decision required")
            if not receipt.get("reason") or not now < receipt.get("nextReviewAt", 0) <= self.contract["deadline"]:
                raise ValueError("reason and bounded next review required")
            event.update(decision=receipt["decision"], nextReviewAt=receipt["nextReviewAt"])
        if stage == "action_verified":
            if event.get("decision") == "dispatch":
                if not evidence_ref(receipt.get("dispatchEvidence")) or not evidence_ref(receipt.get("progressEvidence")):
                    raise ValueError("dispatch AND actual progress evidence required")
                if receipt.get("newTaskBindingId") == event["taskBindingId"] or not receipt.get("newTaskBindingId"):
                    raise ValueError("new authorized task binding required for next batch")
            elif not receipt.get("dependencyVersion"):
                raise ValueError("wait action requires checked dependency version")
        event.update(stage=stage, lastReceiptAt=now, evidence=receipt["evidence"])
        receipts[receipt_id] = {"stage": stage, "payloadHash": payload_hash}
        event.update(receipts=receipts, lastReceiptId=receipt_id)
        if stage == "acknowledged" and not same_stage:
            event["nextReviewAt"] = now + 300
        self._save(revision, state)
        return {"eventId": event["eventId"], "stage": stage,
                "receiptId": receipt_id, "duplicate": False}

    def overdue(self, now):
        revision, state = self._load()
        alerts = []
        for event in state["events"].values():
            if (event["stage"] in {"delivered", "acknowledged", "decision_recorded"}
                    and event["nextReviewAt"] <= now and not event["escalated"]):
                event["escalated"] = True
                alerts.append({"eventId": event["eventId"], "stage": event["stage"],
                               "reason": "owner_followup_overdue", "requiresNonModelNotifier": True})
        self._save(revision, state)
        return alerts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("action", choices=["init", "observe", "prepare", "notification-result", "receipt", "overdue", "status", "stop"])
    args = parser.parse_args(argv)
    contract, _ = read_json(args.contract, 65536)
    store = SupervisionStore(args.db, args.registry)
    now = time.time()
    if args.action == "init":
        result = store.create_run(args.run_id, contract, now)
    else:
        run = store.run_status(args.run_id)
        if run["contract"] != contract:
            raise ValueError("contract drift; no reset or implicit migration")
        engine = Supervisor(store, args.run_id, contract)
        data = read_json(args.input)[0] if args.input else {}
        if args.action == "observe":
            if "polls" in data:
                samples = [public_sample(poll, data["observedAt"], turn_read=data.get("turnReads", {}).get(poll["thread"]["id"]))
                           for poll in data["polls"]]
            else:
                samples = data["samples"]
            result = engine.observe(samples, now)
        elif args.action == "prepare":
            result = engine.prepare_notification(now)
        elif args.action == "notification-result":
            engine.notification_result(data["notificationId"], data["status"], now, data.get("evidence"))
            result = {"recorded": True}
        elif args.action == "receipt":
            result = engine.owner_receipt(data, now)
        elif args.action == "overdue":
            result = engine.overdue(now)
        elif args.action == "stop":
            result = store.stop_run(args.run_id, "explicit_stop")
        else:
            result = {"run": run, "supervision": store.get_record(args.run_id, "supervision")}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "safe": "no automatic dispatch"}))
        raise SystemExit(2)

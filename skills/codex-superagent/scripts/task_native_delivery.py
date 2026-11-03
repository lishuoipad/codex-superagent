"""Durable audit of delivery by the caller's native Codex chat tool.

This module NEVER invokes MCP or starts an app-server. ``begin`` saves an intent
after checking current routing state; it is NOT an atomic permit to send through
a different tool. The caller must still check its actual human instructions and
fresh native state. A pause can arrive between these tools. Accepted, consumed,
and technically completed are separate observations. Unknown sends never replay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from task_runtime import TaskRuntime, _id, _number, _json, verify_ref

PROVIDER = "codex-app-native-message"
WARNING = ("Audit only: begin and the native message tool are separate operations; "
           "there is no cross-tool atomic pause or exactly-once guarantee.")
TABLE = """CREATE TABLE IF NOT EXISTS routing_native_deliveries (
    team_id TEXT NOT NULL, event_id TEXT NOT NULL, provider TEXT NOT NULL,
    prepared_by TEXT NOT NULL, prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
    authority_json TEXT NOT NULL, prepared REAL NOT NULL,
    attempt_token TEXT, attempt_operator TEXT, consumed_json TEXT,
    PRIMARY KEY(team_id,event_id))"""


def _evidence(value):
    if not isinstance(value, dict) or len(_json(value).encode("utf-8")) > 8192:
        raise ValueError("bounded native-tool evidence object required")
    return value


class NativeDelivery:
    def __init__(self, runtime):
        self.rt = runtime

    @staticmethod
    def _installed(con):
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='routing_native_deliveries'").fetchone())

    def _row(self, con, event_id):
        if not self._installed(con):
            raise ValueError("native delivery not prepared")
        row = con.execute("""SELECT e.*,n.provider,n.prepared_by,n.prompt,n.prompt_hash,
            n.attempt_token,n.attempt_operator,n.consumed_json
            FROM routing_events e JOIN routing_native_deliveries n
              ON e.team_id=n.team_id AND e.event_id=n.event_id
            WHERE e.team_id=? AND e.event_id=?""", (self.rt.team_id, event_id)).fetchone()
        if not row or row["provider"] != PROVIDER:
            raise ValueError("event is not owned by the native message provider")
        return row

    @staticmethod
    def _summary(row):
        return {"eventId": row["event_id"], "recipient": row["recipient"],
                "taskBindingId": row["task_id"], "generation": row["generation"],
                "state": row["state"], "attempts": row["attempts"],
                "promptSha256": row["prompt_hash"],
                "accepted": row["state"] == "delivered",
                "consumed": row["consumed_json"] is not None}

    def prepare(self, operator, event, prompt, authorization, now=None):
        """Persist stable routing identity/text. This does not authorize a send.

        ``operator`` is an audit label, not authentication. The calling tool host
        is responsible for authentic human/message authorization.
        """
        _id(operator, "operator")
        proof = verify_ref(authorization)
        now = _number(time.time() if now is None else now)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 16384:
            raise ValueError("nonempty prompt <=16384 UTF-8 bytes required")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        # If another provider already claimed between ingest and attachment,
        # refuse to adopt it. After attachment RPC claim/dispatch excludes it.
        self.rt.ingest(event, now)
        eid = event["eventId"]
        with self.rt._tx() as con:
            con.execute(TABLE)
            native = con.execute("SELECT * FROM routing_native_deliveries WHERE team_id=? AND event_id=?",
                                 (self.rt.team_id, eid)).fetchone()
            if native:
                if native["provider"] != PROVIDER or native["prompt_hash"] != digest:
                    raise ValueError("native event identity reused with different prompt/provider")
                return {"duplicate": True, **self._summary(self._row(con, eid)), "warning": WARNING}
            row = con.execute("SELECT * FROM routing_events WHERE team_id=? AND event_id=?",
                              (self.rt.team_id, eid)).fetchone()
            if row["state"] != "pending" or row["attempts"] != 0:
                raise ValueError("event already observed by another provider; reconcile, do not resend")
            con.execute("INSERT INTO routing_native_deliveries VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                        (self.rt.team_id, eid, PROVIDER, operator, prompt, digest, _json(proof), now))
            con.execute("UPDATE routing_events SET state='native_pending' WHERE team_id=? AND event_id=?",
                        (self.rt.team_id, eid))
            return {"duplicate": False, **self._summary(self._row(con, eid)), "warning": WARNING}

    def begin(self, operator, session_token, event_id, snapshot, now=None):
        """Persist intent; no native call and no transferable send permit."""
        _id(operator, "operator"); _id(session_token, "unique execution sessionToken")
        now = _number(time.time() if now is None else now)
        with self.rt._tx() as con:
            row = self._row(con, event_id)
            team = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.rt.team_id,)).fetchone()
            denied = lambda reason: {"begun": False, "reason": reason, "warning": WARNING}
            if not team["enabled"]:
                return denied("development_paused")
            worker = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND thread_id=?",
                                 (self.rt.team_id, row["recipient"])).fetchone()
            if row["recipient"] != self.rt.router:
                if (not worker or worker["state"] == "completed" or worker["task_id"] != row["task_id"]
                        or worker["generation"] != row["generation"]):
                    if row["state"] == "native_pending":
                        con.execute("UPDATE routing_events SET state='discarded',outcome_json=? WHERE team_id=? AND event_id=?",
                                    (_json({"reason": "superseded_task_binding"}), self.rt.team_id, event_id))
                    return denied("superseded_task_binding")
                if worker["state"] == "paused":
                    return denied("worker_paused")
            if not self.rt._snapshot_ok(snapshot, row["recipient"], now):
                return denied("fresh_idle_native_snapshot_required")
            if team["host_token"] not in {None, session_token} and team["host_until"] > now:
                return denied("other_dispatcher_lease_active")
            # A different/expired invocation may have crossed the external API.
            # Never infer not_sent from timeout or expiry, including same token.
            if team["host_until"] <= now or team["host_token"] != session_token:
                con.execute("UPDATE routing_events SET state='unknown',outcome_json=? WHERE team_id=? AND state='inflight'",
                            (_json({"reason": "dispatcher_lease_ended_without_outcome"}), self.rt.team_id))
                row = self._row(con, event_id)
            if row["state"] != "native_pending":
                return denied("event_" + row["state"])
            if row["not_before"] > now:
                return denied("retry_not_before")
            if row["attempts"] >= 3:
                return denied("native_attempt_limit")
            if con.execute("SELECT 1 FROM routing_events WHERE team_id=? AND recipient=? AND state IN ('inflight','unknown')",
                           (self.rt.team_id, row["recipient"])).fetchone():
                return denied("recipient_has_unresolved_delivery")
            attempt = uuid.uuid4().hex
            con.execute("UPDATE routing_teams SET host_token=?,host_until=? WHERE team_id=?",
                        (session_token, now + 120, self.rt.team_id))
            con.execute("UPDATE routing_events SET state='inflight',lease_token=?,gate_revision=?,attempts=attempts+1 "
                        "WHERE team_id=? AND event_id=?", (session_token, team["revision"], self.rt.team_id, event_id))
            con.execute("UPDATE routing_native_deliveries SET attempt_token=?,attempt_operator=? WHERE team_id=? AND event_id=?",
                        (attempt, operator, self.rt.team_id, event_id))
            return {"begun": True, "eventId": event_id, "attemptToken": attempt,
                    "recipient": row["recipient"], "hostId": self.rt.host_id,
                    "taskBindingId": row["task_id"], "generation": row["generation"],
                    "gateRevision": team["revision"], "checkedAt": now,
                    "nativeTool": "send_message_to_thread", "prompt": row["prompt"],
                    "promptSha256": row["prompt_hash"], "warning": WARNING}

    def finish(self, operator, event_id, attempt_token, status, evidence, now=None):
        """Record the actual native tool outcome; acceptance is not consumption."""
        if status not in {"accepted", "not_sent", "unknown"}:
            raise ValueError("status must be accepted, not_sent, or unknown")
        evidence = _evidence(evidence)
        now = _number(time.time() if now is None else now)
        with self.rt._tx() as con:
            row = self._row(con, event_id)
            if row["attempt_token"] != attempt_token or row["attempt_operator"] != operator:
                raise ValueError("native outcome does not own attempt")
            if row["state"] != "inflight":
                raise ValueError("attempt no longer inflight; inspect history and reconcile")
            if status == "accepted":
                state, not_before = "delivered", row["not_before"]
            elif status == "not_sent" and row["attempts"] < 3:
                state, not_before = "native_pending", now + min(60, 2 ** row["attempts"])
            else:
                state, not_before = "unknown", row["not_before"]
            outcome = {"provider": PROVIDER, "nativeStatus": status, "observedAt": now, "evidence": evidence}
            con.execute("UPDATE routing_events SET state=?,not_before=?,outcome_json=? WHERE team_id=? AND event_id=?",
                        (state, not_before, _json(outcome), self.rt.team_id, event_id))
            # Release only this execution's lease and only after all its sends.
            if not con.execute("SELECT 1 FROM routing_events WHERE team_id=? AND state='inflight' AND lease_token=?",
                               (self.rt.team_id, row["lease_token"])).fetchone():
                con.execute("UPDATE routing_teams SET host_token=NULL,host_until=0 WHERE team_id=? AND host_token=?",
                            (self.rt.team_id, row["lease_token"]))
            return self._summary(self._row(con, event_id))

    def receipt(self, operator, event_id, recipient, task_binding_id, consumer_turn_id, evidence_ref, now=None):
        _id(operator, "operator"); _id(consumer_turn_id, "consumerTurnId")
        proof = verify_ref(evidence_ref)
        now = _number(time.time() if now is None else now)
        with self.rt._tx() as con:
            row = self._row(con, event_id)
            if row["state"] != "delivered":
                raise ValueError("consumption requires an accepted or reconciled native delivery")
            if row["recipient"] != recipient or row["task_id"] != task_binding_id:
                raise ValueError("receipt recipient/task mismatch")
            receipt = {"operator": operator, "recipient": recipient, "taskBindingId": task_binding_id,
                       "consumerTurnId": consumer_turn_id, "evidenceRef": proof}
            if row["consumed_json"] is not None:
                prior = json.loads(row["consumed_json"])
                if {k: prior[k] for k in receipt} != receipt:
                    raise ValueError("consumption receipt already recorded; preserve original evidence")
                return {"duplicate": True, **self._summary(row)}
            receipt["observedAt"] = now
            con.execute("UPDATE routing_native_deliveries SET consumed_json=? WHERE team_id=? AND event_id=?",
                        (_json(receipt), self.rt.team_id, event_id))
            return {"duplicate": False, **self._summary(self._row(con, event_id))}

    def reconcile(self, actor, event_id, status, authorization, evidence, now=None):
        self.rt._actor(actor)
        proof = verify_ref(authorization)
        if status not in {"accepted", "not_sent"}:
            raise ValueError("reconcile requires proven accepted or proven not_sent")
        now = _number(time.time() if now is None else now)
        evidence = _evidence(evidence)
        with self.rt._tx() as con:
            row = self._row(con, event_id)
            expired_inflight = False
            if row["state"] == "inflight":
                team = con.execute("SELECT host_until FROM routing_teams WHERE team_id=?", (self.rt.team_id,)).fetchone()
                if team["host_until"] > now:
                    raise ValueError("native attempt lease still active; do not reconcile a running sender")
                # There may be no later event/host acquisition to observe this
                # crash. Explicit reconciliation can recover it, but only after
                # lease expiry AND actual native-history inspection by caller.
                con.execute("UPDATE routing_events SET state='unknown' WHERE team_id=? AND event_id=?",
                            (self.rt.team_id, event_id))
                expired_inflight = True
            elif row["state"] != "unknown":
                raise ValueError("only unknown or expired inflight native delivery can be reconciled")
            state = "delivered" if status == "accepted" else "native_pending"
            # Reconciliation does not replenish the automatic attempt budget.
            con.execute("UPDATE routing_events SET state=?,outcome_json=? WHERE team_id=? AND event_id=?",
                        (state, _json({"provider": PROVIDER, "nativeStatus": status, "observedAt": now,
                                       "expiredInflightRecoveredAsUnknown": expired_inflight,
                                       "authorization": proof, "evidence": evidence}), self.rt.team_id, event_id))
            return self._summary(self._row(con, event_id))

    def status(self, event_id=None):
        with self.rt._tx() as con:
            if event_id is not None:
                return {**self._summary(self._row(con, event_id)), "warning": WARNING}
            if not self._installed(con):
                return {"provider": PROVIDER, "installed": False, "states": {}, "warning": WARNING}
            rows = con.execute("SELECT e.state,count(*) FROM routing_events e JOIN routing_native_deliveries n "
                               "ON e.team_id=n.team_id AND e.event_id=n.event_id WHERE e.team_id=? GROUP BY e.state",
                               (self.rt.team_id,)).fetchall()
            consumed = con.execute("SELECT count(*) FROM routing_native_deliveries WHERE team_id=? AND consumed_json IS NOT NULL",
                                   (self.rt.team_id,)).fetchone()[0]
            pending = con.execute("SELECT e.event_id FROM routing_events e JOIN routing_native_deliveries n "
                                  "ON e.team_id=n.team_id AND e.event_id=n.event_id WHERE e.team_id=? "
                                  "AND e.state IN ('native_pending','inflight','unknown') ORDER BY e.created,e.event_id LIMIT 65",
                                  (self.rt.team_id,)).fetchall()
            return {"provider": PROVIDER, "installed": True, "states": {r[0]: r[1] for r in rows},
                    "consumed": consumed, "pending": [self._summary(self._row(con, r[0])) for r in pending[:64]],
                    "pendingTruncated": len(pending) > 64, "warning": WARNING}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("command", choices=["prepare", "begin", "finish", "receipt", "reconcile", "status"])
    p.add_argument("--input", type=Path)
    args = p.parse_args()
    if not args.config.is_absolute():
        p.error("--config must be absolute")
    rt = TaskRuntime(json.loads(args.config.read_text(encoding="utf-8-sig")))
    native = NativeDelivery(rt)
    data = {}
    if args.input:
        if not args.input.is_absolute() or args.input.stat().st_size > 65536:
            p.error("--input must be an absolute bounded JSON file")
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if args.command == "prepare":
        result = native.prepare(data["operator"], data["event"], data["prompt"], data["authorization"])
    elif args.command == "begin":
        result = native.begin(data["operator"], data["sessionToken"], data["eventId"], data["snapshot"])
    elif args.command == "finish":
        result = native.finish(data["operator"], data["eventId"], data["attemptToken"], data["status"], data["evidence"])
    elif args.command == "receipt":
        result = native.receipt(data["operator"], data["eventId"], data["recipient"], data["taskBindingId"], data["consumerTurnId"], data["evidenceRef"])
    elif args.command == "reconcile":
        result = native.reconcile(data["actor"], data["eventId"], data["status"], data["authorization"], data["evidence"])
    else:
        result = native.status(data.get("eventId"))
    if args.command != "status":
        rt.export()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

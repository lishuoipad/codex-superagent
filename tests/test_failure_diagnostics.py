"""Targeted regression checks for sanitized diagnosis and existing event routing."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-superagent" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import failure_diagnostics as diagnostic
from supervision import Supervisor, public_sample
from failure_diagnostics import capacity_error

THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"


class MemoryStore:
    def __init__(self, contract):
        self.contract = copy.deepcopy(contract)
        self.value = None
        self.revision = 0

    def run_status(self, _run_id):
        return {"contract": copy.deepcopy(self.contract)}

    def get_record(self, _run_id, _key):
        return {"revision": self.revision, "value": copy.deepcopy(self.value)}

    def put_record(self, _run_id, _key, value, revision):
        if revision != self.revision:
            raise ValueError("Fixture revision mismatch")
        self.revision += 1
        self.value = copy.deepcopy(value)


class FailureDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="diagnosis-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = {"threadId": THREAD, "taskBindingId": "task1"}
        self.project = {"known": True, "version": "version1", "record": {
            "taskBindingId": "task1", "taskState": "running", "assignmentState": "running"}}
        self.contract = {"hostId": "local", "owner": "owner1", "targets": [self.target]}
        self.engine = Supervisor(MemoryStore(self.contract), "run1", self.contract)

    def poll(self, message, status="systemError", turn_status="failed", will_retry=False):
        return {"thread": {"id": THREAD, "hostId": "local", "status": {"type": status, "activeFlags": []}},
                "latestTurn": {"id": TURN, "status": turn_status, "startedAt": 1000,
                               "error": {"message": message, "willRetry": will_retry}},
                "cursor": "source1:1", "latestAssistantMessage": {"text": "private-body-do-not-output"}}

    def sample(self, message, **fields):
        return public_sample(self.poll(message, **fields), 1001)

    def classify(self, sample, project=None):
        return self.engine.classify(self.target, sample, project or self.project, {}, 1001)

    def test_real_compaction_network_message(self):
        error = {"message": "Error running remote compact task: stream disconnected before completion: Transport error: network error: error decoding response body"}
        self.assertEqual(diagnostic.classify_error(error)["kind"], "network")
        self.assertEqual(diagnostic.classify_error(error)["phase"], "remote_compaction")
        self.assertFalse(capacity_error(error))

    def test_real_bad_request_message_is_separate(self):
        self.assertEqual(diagnostic.classify_error({"message": '{"detail":"Bad Request"}'})["kind"], "request_rejected")
        result = self.classify(self.sample('{"detail":"Bad Request"}'))
        self.assertEqual(result["state"], "request_rejected")
        self.assertFalse(result["recoveryEligible"])

    def test_primary_protocol_codes_take_precedence(self):
        cases = [
            ({"codexErrorInfo": "usageLimitExceeded", "message": "model is overloaded"}, "usage_limit"),
            ({"codex_error_info": "context_window_exceeded"}, "context_limit"),
            ({"codexErrorInfo": "unauthorized"}, "authentication"),
            ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 400}}, "message": "stream disconnected"}, "request_rejected"),
            ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}, "rate_limit"),
            ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}}}, "service_unavailable"),
            ({"codexErrorInfo": "serverOverloaded"}, "capacity"),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(diagnostic.classify_error(error)["kind"], expected)

    def test_public_sample_discards_body_and_preserves_class(self):
        poll = self.poll("stream disconnected secret-data")
        sample = public_sample(poll, 1001)
        self.assertEqual(sample["failureKind"], "network")
        self.assertNotIn("private-body", json.dumps(sample))
        self.assertNotIn("secret-data", json.dumps(sample))
        self.assertNotIn("error", sample)

    def test_active_and_native_retry_do_not_request_recovery(self):
        for fields in ({"status": "active", "turn_status": "inProgress"}, {"will_retry": True}):
            with self.subTest(fields=fields):
                sample = self.sample("stream disconnected", **fields)
                self.assertIsNone(diagnostic.failure_review(sample))
                self.assertEqual(self.classify(sample)["state"], "active")

    def test_approval_pause_and_terminal_task_have_priority(self):
        for flag in ("waitingOnApproval", "waitingOnUserInput"):
            sample = self.sample("stream disconnected")
            sample["activeFlags"] = [flag]
            self.assertIsNone(diagnostic.failure_review(sample))
            self.assertEqual(self.classify(sample)["state"], "paused_or_needs_human")
        sample = self.sample("stream disconnected")
        sample["paused"] = True
        self.assertEqual(self.classify(sample)["state"], "paused_or_needs_human")
        for state in ("closed", "superseded", "completed"):
            project = copy.deepcopy(self.project)
            project["record"]["taskState"] = state
            self.assertEqual(self.classify(self.sample("stream disconnected"), project)["state"], state)

    def test_fault_review_states_do_not_grant_dispatch(self):
        cases = [("network", "unknown", "transport_failure"),
                 ("network", "remote_compaction", "compaction_failure"),
                 ("context_limit", "unknown", "context_limit_failure"),
                 ("usage_limit", "unknown", "service_needs_action"),
                 ("authentication", "unknown", "service_needs_action"),
                 ("unknown", "unknown", "failure_unknown")]
        for kind, phase, expected in cases:
            sample = self.sample("")
            sample.update(failureKind=kind, failurePhase=phase)
            result = self.classify(sample)
            with self.subTest(kind=kind, phase=phase):
                self.assertEqual(result["state"], expected)
                self.assertFalse(result["recoveryEligible"])

    def test_old_capacity_and_delivery_behavior(self):
        sample = self.sample("")
        sample.pop("failureKind")
        sample.pop("failurePhase")
        sample["capacityFailure"] = True
        self.assertEqual(self.classify(sample)["state"], "capacity_failure")
        project = copy.deepcopy(self.project)
        project["record"]["taskState"] = "delivered_waiting_acceptance"
        result = self.classify(self.sample("stream disconnected", status="active", turn_status="inProgress"), project)
        self.assertEqual(result["state"], "delivery_needs_decision")

    def test_stale_wrong_host_and_missing_project_stay_unknown(self):
        for field, value in (("observedAt", 0), ("hostId", "another-host"), ("readError", True)):
            sample = self.sample("stream disconnected")
            sample[field] = value
            self.assertEqual(self.classify(sample)["state"], "unknown")
        self.assertEqual(self.classify(self.sample("stream disconnected"), {"known": False})["state"], "unknown")

    def test_existing_reducer_deduplicates_same_failed_turn(self):
        sample = self.sample("stream disconnected")
        self.engine.observe([sample], 1001, records={THREAD: self.project})
        sample.update(cursor="source1:2", observedAt=1002)
        self.engine.observe([sample], 1002, records={THREAD: self.project})
        self.assertEqual(len(self.engine.store.value["events"]), 1)
        event = next(iter(self.engine.store.value["events"].values()))
        self.assertEqual(event["sourceVersion"], TURN)
        self.assertEqual(event["reason"], "failure_needs_checked_recovery")

    def create_logs(self):
        path = self.root / "logs.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, level TEXT, target TEXT, feedback_log_body TEXT, thread_id TEXT)")
            rows = [(1, 1001, 0, "WARN", "codex_core::responses_retry", "turn_id="+TURN+" request=Sampling request_id=req_12345678 stream disconnected token=top-secret", THREAD),
                    (2, 1001, 0, "WARN", "other", "turn_id=another-turn stream disconnected", THREAD),
                    (3, 1001, 0, "WARN", "other", "turn_id="+TURN+" stream disconnected", "another-thread")]
            connection.executemany("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", rows)
        return path

    def active_fixture(self):
        poll = self.poll("", status="active", turn_status="inProgress")
        marker = "exec-test"
        poll["latestToolMarkerId"] = marker
        poll["latestToolMarker"] = {"id": marker, "turnId": TURN, "type": "commandExecution", "status": "completed"}
        read = {"thread": {"id": THREAD, "hostId": "local"}, "turns": [{"id": TURN, "status": "inProgress",
                "items": [{"id": marker, "type": "commandExecution", "status": "completed"}]}]}
        path = self.root / "rollout.jsonl"
        payload = {"type": "item_completed", "thread_id": THREAD, "turn_id": TURN, "started_at_ms": 970000,
                   "completed_at_ms": 980000, "item": {"id": marker, "type": "CommandExecution",
                   "status": "completed", "exit_code": 0}}
        path.write_text(json.dumps({"type": "event_msg", "payload": payload})+"\n", encoding="utf8")
        db = self.create_logs()
        self.target["stallCheck"] = {"logs": str(db), "rollout": str(path), "reviewAfterSeconds": 10}
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("DELETE FROM logs")
            conn.execute("INSERT INTO logs VALUES(1,975,0,'DEBUG','codex_http_client::client',?,?)",
                         ('turn.id="'+TURN+'" Request failed method=POST url=https://chatgpt.com/backend-api/codex/responses error sending request', THREAD))
        self.engine = Supervisor(MemoryStore(self.contract), "run1", self.contract)
        return poll, read, path, db

    def test_active_command_event_is_advisory_and_deduplicated(self):
        poll, read, _, _ = self.active_fixture()
        sample = public_sample(poll, 1001, turn_read=read)
        self.assertEqual(self.classify(sample)["state"], "suspected_stall")
        self.assertFalse(self.classify(sample)["recoveryEligible"])
        self.engine.observe([sample], 1001, records={THREAD: self.project})
        self.engine.observe([dict(sample, observedAt=1002)], 1002, records={THREAD: self.project})
        events = self.engine.store.value["events"]
        self.assertEqual(len(events), 1)
        single = list(events.values())
        self.assertNotIn("before any dispatch", self.engine.message("notification-1", single))
        combined = self.engine.message("notification-2", single + [{"threadId": "other", "reason": "standby_review_due"}])
        self.assertNotIn(THREAD, combined.split("Review the following active turns")[0])
        self.engine.observe([dict(sample, completedCommandId="next", observedAt=1003, cursor="source1:2")],
                            1003, records={THREAD: self.project})
        self.assertEqual(next(iter(self.engine.store.value["events"].values()))["stage"], "closed")

    def test_active_guards_on_native_identity_progress_retry_and_freshness(self):
        poll, read, _, _ = self.active_fixture()
        for label in ("missing", "host", "turn", "busy", "unknownBusy", "paused", "retry", "stale", "flag"):
            p, r = copy.deepcopy(poll), copy.deepcopy(read)
            if label == "host":
                r["thread"]["hostId"] = "other"
            if label == "turn":
                r["turns"][0]["id"] = "other"
            if label in {"busy", "unknownBusy"}:
                r["turns"][0]["items"].append({"type": "commandExecution" if label == "busy" else "newTool",
                                               "status": "inProgress"})
            if label == "retry":
                p["latestTurn"]["willRetry"] = True
            if label == "flag":
                p["thread"]["status"]["activeFlags"] = ["waitingOnUserInput"]
            sample = public_sample(p, 500 if label == "stale" else 1001, turn_read=None if label=="missing" else r)
            sample["paused"] = label == "paused"
            with self.subTest(label=label):
                self.assertNotEqual(self.classify(sample)["state"], "suspected_stall")

    def test_notification_requires_authority_and_fresh_native_review(self):
        poll, read, _, _ = self.active_fixture()
        self.contract.update(deadline=2000, policy={"coalesceWindowSeconds": 0})
        sample = public_sample(poll, 1001, turn_read=read)
        for authorized in (False, True):
            self.contract["messageAuthority"] = {"authorized": authorized}
            store = MemoryStore(self.contract)
            def reserve(run_id, notification_id, owner, ids, now):
                self.assertEqual(owner, "owner1")
                self.assertEqual(len(ids), 1)
                return {"allowed": True, "intentId": "fixture-intent"}
            store.reserve_notification = reserve
            engine = Supervisor(store, "run1", self.contract)
            engine.observe([sample], 1001, records={THREAD: self.project})
            self.assertFalse(engine.prepare_notification(1122)["allowed"])
            notice = engine.prepare_notification(1001)
            self.assertEqual(notice["allowed"], authorized)
            if authorized:
                self.assertEqual(notice["events"][0]["stallEvidence"]["commandId"], "exec-test")
                self.assertNotIn("before any dispatch", notice["message"])
                self.assertFalse(engine.prepare_notification(1001)["allowed"])

    def test_newer_native_transport_result_supersedes_failure(self):
        poll, read, _, db = self.active_fixture()
        sample = public_sample(poll, 1001, turn_read=read)
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("INSERT INTO logs VALUES(2,981,0,'DEBUG','codex_http_client::client',?,?)",
                         ('turn.id="'+TURN+'" Request completed method=POST status=200', THREAD))
        self.assertEqual(self.classify(sample)["state"], "active")

    def test_new_partial_and_oversized_last_record_stay_active(self):
        poll, read, path, _ = self.active_fixture()
        sample = public_sample(poll, 1001, turn_read=read)
        old = path.read_bytes()
        for new in (b'{"type":"progress"}\n', b'{', b'x'*131073+b'\n'):
            path.write_bytes(old+new)
            self.assertEqual(self.classify(sample)["state"], "active")

    def test_http_400_exports_selected_ids_only(self):
        _, _, _, db = self.active_fixture()
        for status, index in ((200, 2), (400, 3)):
            msg = ('turn.id="'+TURN+'"}: Request completed method=POST url=https://chatgpt.com/backend-api/codex/responses status='
                   +str(status)+' Server status headers='+json.dumps({"cf-ray": "0123456789abcdef-XYZ",
                   "x-request-id": "req_12345678", "set-cookie": "do-not-export"}))
            with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("INSERT INTO logs VALUES(?,?,0,'DEBUG','codex_http_client::client',?,?)", (index, 980+index, msg, THREAD))
        rows = diagnostic.log_metadata(db, THREAD, TURN, 970, 10)
        self.assertEqual([r["logId"] for r in rows], [3])
        self.assertEqual(rows[0]["error"], {"kind":"request_rejected", "phase":"unknown", "httpStatus":400})
        self.assertEqual(rows[0]["requestId"], "req_12345678")
        self.assertEqual(rows[0]["cfRay"], "0123456789abcdef-XYZ")
        self.assertNotIn("do-not-export", json.dumps(rows))

    def test_logs_are_scoped_sanitized_and_read_only(self):
        path = self.create_logs()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = diagnostic.log_metadata(path, THREAD, TURN, 1000, 20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requestKind"], "Sampling")
        self.assertEqual(rows[0]["requestId"], "req_12345678")
        self.assertEqual(rows[0]["transport"], "unknown")
        self.assertNotIn("top-secret", json.dumps(rows))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_cli_writes_evidence_once_and_does_not_authorize_dispatch(self):
        source = self.root / "snapshot.json"
        source.write_text(json.dumps({"polls": [self.poll('{"detail":"Bad Request"}')]}), encoding="utf-8")
        output = self.root / "diagnosis.json"
        args = ["--snapshot", str(source), "--thread-id", THREAD, "--logs", str(self.create_logs()), "--output", str(output)]
        with contextlib.redirect_stdout(io.StringIO()):
            diagnostic.main(args)
        report = json.loads(output.read_bytes())
        self.assertEqual(report["failure"]["kind"], "request_rejected")
        self.assertFalse(report["dispatchPermission"])
        self.assertEqual(report["origin"], "undetermined")
        with self.assertRaises(FileExistsError), contextlib.redirect_stdout(io.StringIO()):
            diagnostic.main(args)

    def test_missing_logs_are_unknown_and_other_target_is_rejected(self):
        source = self.root / "snapshot.json"
        source.write_text(json.dumps(self.poll("stream disconnected")), encoding="utf-8")
        output = self.root / "diagnosis.json"
        args = ["--snapshot", str(source), "--thread-id", THREAD, "--logs", str(self.root/"absent.sqlite"), "--output", str(output)]
        with contextlib.redirect_stdout(io.StringIO()):
            diagnostic.main(args)
        self.assertEqual(json.loads(output.read_bytes())["logs"]["status"], "unavailable")
        with self.assertRaises(ValueError):
            diagnostic.select_poll(json.loads(source.read_bytes()), "another-thread")


if __name__ == "__main__":
    unittest.main()

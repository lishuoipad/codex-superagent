"""Frozen nested-reference checks, without model or device calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"
sys.path.insert(0, str(SCRIPTS))
import verify_delivery_refs as verifier


class DeliveryRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "candidate.py"
        self.file.write_bytes(b"known content\n")
        self.ref = {"path": str(self.file), "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest()}
        self.manifest = self.root / "delivery.json"

    def check(self, source=None, **kwargs):
        self.manifest.write_text(json.dumps(source if source is not None else {"files": [self.ref]}), encoding="utf-8")
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        return verifier.verify(self.manifest, sha, **kwargs)

    def test_nested_repeated_refs_hash_once_without_following_documents(self):
        document = self.root / "other.json"
        document.write_text(json.dumps({"path": "unread/secret", "sha256": "a" * 64}), encoding="utf-8")
        other = {"path": str(document), "sha256": hashlib.sha256(document.read_bytes()).hexdigest()}
        result = self.check({"files": [self.ref, other], "review": {"candidate": self.ref}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["refOccurrences"], 3)
        self.assertEqual(result["checkedFiles"], 2)

    def test_wrong_frozen_manifest_hash_reads_no_references(self):
        self.check()
        with mock.patch.object(verifier, "hash_file", side_effect=AssertionError("must not read")):
            result = verifier.verify(self.manifest, "0" * 64)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checkedFiles"], 0)

    def test_mismatch_is_failure(self):
        self.file.write_text("changed", encoding="utf-8")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["files"][0]["error"], "content hash mismatch")

    def test_missing_file_is_failure(self):
        self.file.unlink()
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("error", result["files"][0])

    def test_conflicting_duplicate_is_failure(self):
        result = self.check([self.ref, {**self.ref, "sha256": "b" * 64}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["checkedFiles"], 0)

    def test_relative_and_remote_paths_rejected(self):
        for path in ("relative.py", "//remote/share/file", "\\\\remote\\share\\file"):
            with self.subTest(path=path):
                self.assertFalse(self.check({"files": [{**self.ref, "path": path}]})["ok"])

    def test_malformed_and_empty_refs_cannot_pass(self):
        for source in ({"files": []}, {"sha256": "a" * 64}, {"path": str(self.file), "sha256": "invalid"}):
            with self.subTest(source=source):
                self.assertFalse(self.check(source)["ok"])

    def test_reference_and_read_budgets_fail_closed(self):
        self.assertFalse(self.check([self.ref, self.ref], max_refs=1)["ok"])
        self.assertFalse(self.check(max_bytes=1)["ok"])

    def test_manifest_changed_mid_verification_cannot_pass(self):
        original = verifier.hash_file
        def change(*args):
            result = original(*args)
            self.manifest.write_text("{}", encoding="utf-8")
            return result
        with mock.patch.object(verifier, "hash_file", side_effect=change):
            result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("manifest changed", result["errors"][0])

    def test_path_replaced_mid_read_cannot_pass(self):
        original = verifier.os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                # Windows allows in-place content changes while this file is open.
                with self.file.open("ab") as stream:
                    stream.write(b"changed after read")
            return original(fd)
        with mock.patch.object(verifier.os, "fstat", side_effect=replace):
            result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("file changed", result["files"][0]["error"])

    def test_windows_stat_and_fstat_ctime_difference_is_not_a_false_change(self):
        original = verifier.os.fstat
        def different_ctime(fd):
            value = original(fd)
            fields = {key: getattr(value, key) for key in
                      ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}
            fields["st_ctime_ns"] += 1000
            return SimpleNamespace(**fields)
        with mock.patch.object(verifier.os, "fstat", side_effect=different_ctime):
            self.assertTrue(self.check()["ok"])

    def test_cli_returns_compact_summary_and_preserves_existing_output(self):
        self.check()
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        output = self.root / "report.json"
        cmd = [sys.executable, "-B", str(SCRIPTS / "verify_delivery_refs.py"), str(self.manifest),
               "--expected-sha256", sha, "--report", str(output)]
        first = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        compact = json.loads(first.stdout)
        self.assertTrue(compact["ok"])
        self.assertNotIn("files", compact)
        raw = output.read_bytes()
        self.assertEqual(compact["reportSha256"], hashlib.sha256(raw).hexdigest())
        again = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(output.read_bytes(), raw)



class StorageCloseoutTests(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory(prefix="storage-closeout-", dir=os.environ.get("RELAY_TEST_TMP"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        if os.environ.get("RELAY_TEST_TMP"):
            self.assertEqual(self.root.resolve().parent, Path(os.environ["RELAY_TEST_TMP"]).resolve())
        self.manifest = self.root / "delivery.json"
        self.receipt = self.root / "deletion-receipt.json"
        self.receipt.write_text('{"fixture":"recorded removal, not a real deletion"}', encoding="utf-8")
        self.ref = {"path": str(self.receipt), "sha256": hashlib.sha256(self.receipt.read_bytes()).hexdigest()}
        self.record = {
            "schema": 1, "taskBindingId": "task-a", "workerThreadId": "worker-a",
            "state": "no_cleanup_needed", "reviewedScopes": [str(self.root / "outputs")],
            "removedScopes": [], "releasedBytes": 0, "measurement": "no_removal",
            "retained": [], "note": "This bounded task produced no disposable artifacts.",
            "evidence": []
        }

    def check_record(self, record=None, **kwargs):
        source = {"storageCloseout": self.record if record is None else record,
                  "unrelated": {"path": "never-read-this-other-task", "sha256": "a" * 64}}
        self.manifest.write_text(json.dumps(source), encoding="utf-8")
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        options = {"storage_closeout": True, "task_binding_id": "task-a", "worker_thread_id": "worker-a"}
        options.update(kwargs)
        return verifier.verify(self.manifest, sha, **options)

    def keep(self):
        return {"scope": str(self.root / "shared"), "reason": "Current consumer still needs this input",
                "owner": "worker-b / task-b", "recheckWhen": "After task-b acceptance and repairs"}

    def cleaned(self):
        return {**self.record, "state": "cleaned", "removedScopes": [str(self.root / "old-cache")],
                "releasedBytes": 4096, "measurement": "exclusive_file_allocation", "evidence": [self.ref]}

    def test_no_cleanup_uses_no_reference_reads(self):
        with mock.patch.object(verifier, "hash_file", side_effect=AssertionError("must not hash artifacts")):
            report = self.check_record()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["checkedFiles"], 0)
        self.assertEqual(report["storageCloseout"]["releasedBytes"], 0)

    def test_only_selected_evidence_is_checked(self):
        report = self.check_record(self.cleaned())
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["checkedFiles"], 1)
        self.assertEqual(report["files"][0]["path"], str(self.receipt.resolve()))
        self.assertEqual(report["storageCloseout"]["measurement"], "exclusive_file_allocation")

    def test_legitimate_retention_passes_without_statting_retained_paths(self):
        record = {**self.record, "state": "retained", "retained": [self.keep()]}
        report = self.check_record(record)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["storageCloseout"]["retainedCount"], 1)
        self.assertFalse((self.root / "shared").exists())

    def test_partial_blocked_cleanup_remains_explicitly_pending(self):
        record = {**self.cleaned(), "state": "blocked", "retained": [self.keep()],
                  "note": "The remaining removal was denied; consumer owner will follow up."}
        report = self.check_record(record)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["storageCloseout"]["cleanupPending"])

    def test_unmeasured_removal_uses_unknown_not_an_estimate(self):
        report = self.check_record({**self.cleaned(), "measurement": "unmeasured", "releasedBytes": None})
        self.assertTrue(report["ok"], report)
        self.assertIsNone(report["storageCloseout"]["releasedBytes"])
        self.assertFalse(self.check_record({**self.cleaned(), "measurement": "unmeasured"})["ok"])

    def test_wrong_task_or_worker_is_rejected_before_evidence_reads(self):
        for key in ("taskBindingId", "workerThreadId"):
            record = {**self.cleaned(), key: "unrelated"}
            with self.subTest(key=key), mock.patch.object(verifier, "hash_file", side_effect=AssertionError("must not read")):
                report = self.check_record(record)
                self.assertFalse(report["ok"])
                self.assertEqual(report["checkedFiles"], 0)

    def test_missing_closeout_record_cannot_pass(self):
        self.manifest.write_text('{"result":"done"}', encoding="utf-8")
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        report = verifier.verify(self.manifest, sha, storage_closeout=True, task_binding_id="task-a", worker_thread_id="worker-a")
        self.assertFalse(report["ok"])

    def test_retention_requires_reason_owner_and_trigger(self):
        for key in ("scope", "reason", "owner", "recheckWhen"):
            retained = self.keep()
            retained.pop(key)
            with self.subTest(key=key):
                self.assertFalse(self.check_record({**self.record, "state": "retained", "retained": [retained]})["ok"])

    def test_logical_and_volume_delta_metrics_are_rejected(self):
        for metric in ("logical_bytes", "volume_free_delta", "estimated", "no_removal"):
            with self.subTest(metric=metric):
                self.assertFalse(self.check_record({**self.cleaned(), "measurement": metric})["ok"])

    def test_measured_bytes_reject_bool_negative_float_and_missing(self):
        for value in (True, -1, 1.5, None, "4096"):
            with self.subTest(value=value):
                self.assertFalse(self.check_record({**self.cleaned(), "releasedBytes": value})["ok"])

    def test_no_removal_cannot_claim_release(self):
        for value in (True, 1, None):
            with self.subTest(value=value):
                self.assertFalse(self.check_record({**self.record, "releasedBytes": value})["ok"])

    def test_claimed_removal_requires_verifiable_evidence(self):
        for refs in ([], [{}], [{"path": str(self.receipt), "sha256": "not-a-hash"}]):
            with self.subTest(refs=refs):
                self.assertFalse(self.check_record({**self.cleaned(), "evidence": refs})["ok"])
        self.receipt.write_text("changed", encoding="utf-8")
        self.assertFalse(self.check_record(self.cleaned())["ok"])

    def test_missing_review_note_and_inconsistent_states_are_rejected(self):
        invalid = [
            {**self.record, "reviewedScopes": []},
            {**self.record, "note": ""},
            {**self.record, "state": "cleaned"},
            {**self.record, "state": "retained"},
            {**self.record, "state": "blocked"},
            {**self.record, "state": "no_cleanup_needed", "retained": [self.keep()]},
            {**self.record, "schema": True}
        ]
        for record in invalid:
            with self.subTest(record=record):
                self.assertFalse(self.check_record(record)["ok"])

    def test_same_scope_cannot_be_removed_and_retained(self):
        record = self.cleaned()
        retained = self.keep()
        for scope in (record["removedScopes"][0], str(self.root) + "/./old-cache/"):
            with self.subTest(scope=scope):
                retained["scope"] = scope
                self.assertFalse(self.check_record({**record, "retained": [retained]})["ok"])

    def test_retained_parent_with_removed_child_does_not_force_extra_deletion(self):
        retained = {**self.keep(), "scope": str(self.root)}
        self.assertTrue(self.check_record({**self.cleaned(), "retained": [retained]})["ok"])

    def test_storage_cli_requires_expected_identities_and_keeps_compact_report(self):
        self.check_record()
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        output = self.root / "storage-report.json"
        args = [sys.executable, "-B", str(SCRIPTS / "verify_delivery_refs.py"), str(self.manifest),
                "--expected-sha256", sha, "--storage-closeout", "--report", str(output)]
        missing = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(output.exists())
        result = subprocess.run(args + ["--task-binding-id", "task-a", "--worker-thread-id", "worker-a"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["storageCloseout"]["state"], "no_cleanup_needed")
        self.assertNotIn("files", summary)
        self.assertEqual(summary["reportSha256"], hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()

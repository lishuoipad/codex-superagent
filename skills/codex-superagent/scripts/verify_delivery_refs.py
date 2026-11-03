"""Read-only verification of existing nested {path, sha256} delivery records.

Checks file identity/content only. No installation, acceptance, recursive file
discovery, following referenced manifests, model calls or business testing.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

# Bounded reader retained locally; no legacy supervision/recovery dependency.
RECORD_LIMIT = 2 * 1024 * 1024

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


def fingerprint(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def local_file(value):
    p = Path(value)
    resolved = p.resolve()
    # Evidence verification must not start a remote file transfer.
    name = str(resolved)
    if name.startswith("\\\\?\\") and not name.lower().startswith("\\\\?\\unc\\"):
        name = name[4:]
    if not p.is_absolute() or name.startswith(("\\\\", "//")):
        raise ValueError("absolute local file path required")
    return p


def hash_file(path, remaining_bytes):
    p = local_file(path)
    before = p.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("regular file required")
    if before.st_size > remaining_bytes:
        raise ValueError("total read budget exceeded")
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # Windows stat/fstat can expose different ctime semantics. Compare
        # identity/size/mtime across APIs, then ctime within each API only.
        if fingerprint(before)[:-1] != fingerprint(opened)[:-1]:
            raise ValueError("file changed before read")
        while chunk := stream.read(min(1024 * 1024, remaining_bytes - size + 1)):
            size += len(chunk)
            if size > remaining_bytes:
                raise ValueError("total read budget exceeded during read")
            h.update(chunk)
        after = os.fstat(stream.fileno())
    if fingerprint(opened) != fingerprint(after) or fingerprint(before) != fingerprint(p.stat()):
        raise ValueError("file changed during read")
    return h.hexdigest(), size


def storage_closeout_section(source, task_binding_id, worker_thread_id):
    """Validate a task-bound disposition record, never discover or delete files."""
    def required_text(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ValueError(label + " must be nonempty bounded text")
        return value

    def scopes(value, label):
        if not isinstance(value, list) or len(value) > 128:
            raise ValueError(label + " must be a bounded list")
        for item in value:
            required_text(item, label + " item")
        return value

    required_text(task_binding_id, "expected task binding")
    required_text(worker_thread_id, "expected Worker identity")
    if not isinstance(source, dict) or not isinstance(source.get("storageCloseout"), dict):
        raise ValueError("storageCloseout record required in the existing delivery")
    record = source["storageCloseout"]
    if type(record.get("schema")) is not int or record["schema"] != 1:
        raise ValueError("unsupported storageCloseout schema")
    if record.get("taskBindingId") != task_binding_id or record.get("workerThreadId") != worker_thread_id:
        raise ValueError("storageCloseout task or Worker identity mismatch")
    reviewed = scopes(record.get("reviewedScopes"), "reviewedScopes")
    if not reviewed:
        raise ValueError("reviewedScopes must identify the reviewed task outputs")
    removed = scopes(record.get("removedScopes"), "removedScopes")
    required_text(record.get("note"), "storageCloseout note")
    state = record.get("state")
    if state not in {"cleaned", "no_cleanup_needed", "retained", "blocked"}:
        raise ValueError("unknown storageCloseout state")
    retained = record.get("retained")
    if not isinstance(retained, list) or len(retained) > 128:
        raise ValueError("retained must be a bounded list")
    for item in retained:
        if not isinstance(item, dict):
            raise ValueError("retained item must be an object")
        for key in ("scope", "reason", "owner", "recheckWhen"):
            required_text(item.get(key), "retained." + key)
    # Lexical comparison only: removed paths need not exist and retained paths
    # must not be scanned. Parent retention can legitimately include removed children.
    removed_keys = {os.path.normcase(os.path.normpath(scope.strip())) for scope in removed}
    if any(os.path.normcase(os.path.normpath(item["scope"].strip())) in removed_keys for item in retained):
        raise ValueError("the same scope cannot be both removed and retained")
    if state == "cleaned" and not removed:
        raise ValueError("cleaned state needs removedScopes")
    if state == "no_cleanup_needed" and (removed or retained):
        raise ValueError("no_cleanup_needed cannot hide removals or retained work")
    if state == "retained" and (removed or not retained):
        raise ValueError("retained state needs retained scopes and no removals")
    if state == "blocked" and not retained:
        raise ValueError("blocked state needs an owner and recheck trigger")
    released = record.get("releasedBytes")
    metric = record.get("measurement")
    if removed:
        if metric == "exclusive_file_allocation":
            if type(released) is not int or released < 0:
                raise ValueError("releasedBytes must be nonnegative measured allocation")
        elif metric == "unmeasured":
            if released is not None:
                raise ValueError("unmeasured releases must use null, not estimated bytes")
        else:
            raise ValueError("removals need exclusive_file_allocation or unmeasured")
    elif type(released) is not int or released != 0 or metric != "no_removal":
        raise ValueError("no removal means releasedBytes=0 and measurement=no_removal")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 128:
        raise ValueError("evidence must be a bounded list")
    for ref in evidence:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
            raise ValueError("storage evidence must use path+sha256 references")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", ref["sha256"]):
            raise ValueError("storage evidence requires a SHA256")
    if removed and not evidence:
        raise ValueError("reported removals require existing evidence references")
    summary = {"state": state, "taskBindingId": task_binding_id, "workerThreadId": worker_thread_id,
               "reviewedScopeCount": len(reviewed), "removedScopeCount": len(removed),
               "releasedBytes": released, "measurement": metric, "retainedCount": len(retained),
               "cleanupPending": state == "blocked",
               "meaning": "record completeness and referenced bytes only; not deletion authorization or proof of reclamation"}
    return record, summary


def verify(manifest, expected_sha256, max_bytes=128 * 1024 * 1024, max_refs=1024,
           *, storage_closeout=False, task_binding_id=None, worker_thread_id=None):
    """Only the supplied JSON is traversed; referenced files are hashed once."""
    report = {"schemaVersion": 1, "manifest": str(manifest), "expectedSha256": expected_sha256,
              "scope": "nested path+sha256 refs; content identity only, not acceptance",
              "ok": False, "refOccurrences": 0, "checkedFiles": 0, "completedReadBytes": 0,
              "files": [], "errors": []}
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("expected manifest SHA256 required")
        if max_bytes <= 0 or max_refs <= 0:
            raise ValueError("positive read budgets required")
        p = local_file(manifest)
        source, actual = read_json(p)
        report["manifestSha256"] = actual
        if actual != expected_sha256.lower():
            raise ValueError("manifest hash mismatch; no references read")
        target = source
        if storage_closeout:
            target, disposition = storage_closeout_section(source, task_binding_id, worker_thread_id)
            report["scope"] = "storageCloseout fields and referenced evidence only; not deletion or business acceptance"
            report["storageCloseout"] = disposition
        refs = {}

        def visit(value, depth=0):
            if depth > 32:
                raise ValueError("manifest nesting limit exceeded")
            if isinstance(value, dict):
                if "sha256" in value:
                    path, sha = value.get("path"), value["sha256"]
                    if not isinstance(path, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                        raise ValueError("malformed path+sha256 reference")
                    report["refOccurrences"] += 1
                    if report["refOccurrences"] > max_refs:
                        raise ValueError("reference limit exceeded")
                    normalized = str(local_file(path).resolve())
                    key = os.path.normcase(normalized)
                    previous = refs.get(key)
                    if previous and previous[1] != sha.lower():
                        raise ValueError("conflicting hashes for reference: " + normalized)
                    refs[key] = normalized, sha.lower()
                for child in value.values():
                    visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)

        visit(target)
        if not refs and not storage_closeout:
            raise ValueError("no path+sha256 references found")
        for path, expected in refs.values():
            row = {"path": path, "expectedSha256": expected, "ok": False}
            try:
                actual, count = hash_file(path, max_bytes - report["completedReadBytes"])
                row.update(actualSha256=actual, bytes=count, ok=actual == expected)
                report["completedReadBytes"] += count
                report["checkedFiles"] += 1
                if actual != expected:
                    row["error"] = "content hash mismatch"
            except (OSError, ValueError) as exc:
                row["error"] = str(exc)
                # A failed read may have consumed an unknown part of the budget.
                # Stop rather than resetting its budget and continuing.
                report["files"].append(row)
                break
            report["files"].append(row)
        _, final_sha = read_json(p)
        if final_sha != report["manifestSha256"]:
            raise ValueError("manifest changed during verification")
        report["ok"] = len(report["files"]) == len(refs) and all(row["ok"] for row in report["files"])
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        report["errors"].append(str(exc))
    return report


def summary(report):
    issues = report["errors"] + [row for row in report["files"] if not row["ok"]]
    return {**{k: v for k, v in report.items() if k not in {"files", "errors"}},
            "issueCount": len(issues), "issues": issues[:10], "truncated": len(issues) > 10}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--report", required=True, help="new report path in the existing task output")
    parser.add_argument("--max-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--max-refs", type=int, default=1024)
    parser.add_argument("--storage-closeout", action="store_true",
                        help="check only storageCloseout in an existing delivery; never delete")
    parser.add_argument("--task-binding-id")
    parser.add_argument("--worker-thread-id")
    args = parser.parse_args()
    if args.storage_closeout and (not args.task_binding_id or not args.worker_thread_id):
        parser.error("--storage-closeout requires both expected task and Worker identities")
    if not args.storage_closeout and (args.task_binding_id or args.worker_thread_id):
        parser.error("identity arguments require --storage-closeout")
    output = local_file(args.report)
    # Reserve a new output before reading; never overwrite delivery/evidence.
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        report = verify(args.manifest, args.expected_sha256, args.max_bytes, args.max_refs,
                        storage_closeout=args.storage_closeout, task_binding_id=args.task_binding_id,
                        worker_thread_id=args.worker_thread_id)
        raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        stream.write(raw)
    print(json.dumps({**summary(report), "report": str(output),
                      "reportSha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

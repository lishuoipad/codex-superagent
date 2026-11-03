# Evidence and shared resources

Make plans from the actual request, current implementation, real entry points and valid recent evidence. Distinguish facts, inferences and untested hypotheses. Prefer the smallest change addressing a demonstrated gap; do not add another platform or ledger for completeness.

## Reuse and verification

Preserve original artifacts. A matching hash proves matching bytes, not correct semantics or current execution. Reuse a passing result only when the relevant source, dependencies, configuration, fixture and running conditions are unchanged.

Retest failures, affected callers and coverage that is missing. Shared-component changes require checking actual callers; an unrelated whole-repository hash change does not invalidate every previous result.

The bundled `scripts/verify_delivery_refs.py` checks nested local `path + sha256` references. Consult its `--help` and existing project invocation before use. Save review progress and specific dependencies in the existing record, tied to the delivery version. Do not treat a matching reference graph as acceptance.

## Visuals and uploads

Use bounded tool output, explicit truncation notices and artifact references. Batch independent reads. Keep error context and enough evidence to reproduce the issue.

Start from structured runtime diagnostics when useful. A matching image hash or local diff cannot replace the first visual review, changed UI checks or required end-to-end device evidence. Reusing an earlier visual conclusion also requires evidence of the current operation and state.

This package supplies workflow guidance. It does not compress model requests, remove old image context, eliminate uploads or keep a WebSocket alive.

## Exclusive resources

Hidden windows, separate ports and separate worktrees do not isolate system mouse/keyboard input. Use existing input leases and device ownership. Never overwrite another running task's writable fixture or staging directory.

Keep saving, account, asset and service safeguards. Changes affecting real data need the relevant test authority and safe fixture. A process disappearing is not proof that data was saved.

Publish only files the user authorized for publication. Review the exact outgoing content and repository identity; remove credentials, player/customer data, live profiles, real chat IDs and screenshots with private information from public examples.

## Storage closeout

For projects that already require cleanup, put a `storageCloseout` object in the existing delivery JSON; do not start another artifact ledger. The field shape is:

~~~json
{
  "storageCloseout": {
    "schema": 1,
    "taskBindingId": "current-complete-task",
    "workerThreadId": "current-worker",
    "state": "retained",
    "reviewedScopes": ["/absolute/task/build"],
    "removedScopes": [],
    "releasedBytes": 0,
    "measurement": "no_removal",
    "retained": [{
      "scope": "/absolute/task/build",
      "reason": "Still used by the current consumer",
      "owner": "Actual consumer and its task/batch",
      "recheckWhen": "After that consumer's acceptance and repairs close"
    }],
    "note": "No currently safe deletion; the dependency has an owner.",
    "evidence": []
  }
}
~~~

~~~text
python /installed/skill/scripts/verify_delivery_refs.py /existing/task/delivery.json --expected-sha256 <frozen-sha> --storage-closeout --task-binding-id <current-task> --worker-thread-id <current-worker> --report /existing/task/evidence/new-storage-check.json
~~~

States: `cleaned` requires removed scopes and references to existing deletion evidence; `no_cleanup_needed` has no removals or pending retained work and explains why; `retained` lists retained scopes without removals; `blocked` may contain partial removals but keeps unresolved items with a reason, owner and recheck trigger. Include reviewed scopes and a concrete note in all cases.

No removals means `releasedBytes: 0` and `measurement: "no_removal"`. Use `exclusive_file_allocation` for measured nonnegative allocated bytes, accounting for hardlinks/compression. Otherwise use `releasedBytes: null` and `measurement: "unmeasured"`. Do not substitute logical file sizes or whole-volume free-space changes. Shared retained outputs must have an actual consumer/task to handle their later closeout through existing events; do not reopen completed old bindings.

This mode checks only the selected section and its direct small evidence references, not unrelated delivery references or artifact trees. It does not read APKs/large archives, follow referenced manifests, delete anything, schedule cleanup or authorize filesystem changes. A zero exit code means a complete record and matching referenced bytes, not verified reclamation or business acceptance. A valid `blocked` record explicitly returns `cleanupPending: true`. Missing records affect that delivery's closeout, not unrelated ready work.

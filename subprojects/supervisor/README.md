# RealStop Supervisor: experimental evidence-aware continuation

Supervisor is a standalone RealStop subproject. It is not installed in the live hooks or included in the published release. Its potential contribution is combining captured evidence, scoped permissions, artifact checks, native confirmation, and bounded retries. Existing goal controllers already address persistence; an incremental outcome benefit has not yet been established.

## Implemented components

- [Capture adapters](PROVIDER_SCHEMAS.md) record scoped Claude/Codex hook payloads in a private [SQLite ledger](../../realstop/supervisor/ledger.py), with explicit gaps and tool-call correlation. Claude capture has been exercised in controlled real sessions; Codex real-host capture still needs normal hook trust activation.
- [Opt-in contracts](CONTRACTS.md) accept bounded structured declarations from the sole captured user prompt of a host-attested fresh scope. Registered operations constrain requested permissions; declarations cannot establish complete authority, produce receipts, or execute actions.
- [Policy](POLICY.md) separates completion, authorization, feasibility, and coverage. Trusted typed obligations and revision-bound receipts determine whether native review is eligible.
- [Native review](NATIVE_REVIEW.md) binds the complete evidence packet and selected action to a restricted Claude or Codex response. Confirmation requires exact source references.
- [Coordinator](COORDINATOR.md) resolves captured source roles, includes contrary evidence, rejects stale snapshots, and atomically records a single intervention claim. A claim is advisory; no continuation action is executed.
- [Artifact checks](ARTIFACT_CHECKS.md) read bounded regular files and produce point-in-time hash observations. [Local observation sources](LOCAL_OBSERVATIONS.md) retain those checks separately from native hooks, bind scope and registry metadata, and recheck files before saving advisory claims. Root selection remains trusted host input; changed historical evidence can conservatively block a claim.
- [Progress assessment](PROGRESS.md) counts verified work units separately from legitimate waits and stop limits.
- [Optional Verdict adapter](VERDICT_EVALUATION.md) supplies advisory tool-outcome classifications. It is disabled by default; no weights are downloaded automatically and no advantage over simple parsing has been demonstrated.
- [Synthetic mechanism check](MECHANISM_EVALUATION.md) exercises a checker-to-review path with one native call and two deterministic controls; it does not establish live effectiveness.
- [Original work-unit fixtures](benchmarks/work_units/README.md) score actual artifacts, stale receipts, partial completion, and legitimate waits. These are public calibration tasks, not held-out accuracy evidence.

## Boundaries that still matter

Application code must supply authenticated obligations, permissions, and artifact facts. Automatic extraction of those facts from arbitrary conversations is not implemented. Hook records alone do not prove complete capture or that an external artifact remains current. Stop and session-end callbacks can lack unique invocation identities; these remain explicitly uncertain rather than being deduplicated by prompt ID.

The coordinator conservatively abstains on capture gaps, unresolved tool calls, and scopes exceeding its 256-event or 32,000-byte evidence budget. Consequently it does not yet protect general long-running sessions. A controlled fresh-session experiment exercised capture, a completed artifact contract, and native rejection of an unauthorized continuation; it did not establish real-world quit-recovery accuracy.

SQLite serializes local claims, not external actions. Store the database on a trusted local filesystem: new directories use mode 0700 and databases mode 0600, with rollback journals. Original JSON is preserved semantically and remains private. Metadata identifiers can also be sensitive.

## Local checks

```sh
python3 -m unittest discover -s subprojects/supervisor/tests -v
python3 -m unittest discover -s subprojects/supervisor/benchmarks/work_units -p "test_*.py" -v
python3 -m realstop.supervisor.ledger status /private/path/ledger.sqlite3 codex session-id task-id
```

See [design](DESIGN.md), [evaluation requirements](EVALUATION.md), and [source/license inventory](REUSE.md). No model-accuracy, production-readiness, or commercial-demand claim is made.

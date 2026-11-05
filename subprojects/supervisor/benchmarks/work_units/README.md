# Executable original work-unit calibration

This benchmark supplies small, externally inspectable artifact checks for later
controlled agent-outcome experiments. It contains eight original fictional tasks
across text transformation, structured editing, fixed CSV arithmetic, copy integrity,
partial bundles, claim idempotency, revision receipts and legitimate waits. It does
not run agents/models, train anything, compare controllers, or establish accuracy.
These are public calibration fixtures, not a holdout or production distribution.

The expected artifacts are frozen in `tasks.json`; `FROZEN.json` records its SHA256
and the schema hash before any agent execution. `runner.py` refuses a changed
manifest/schema unless the explicit freeze is updated. Any such change creates a
new benchmark version for an outcome comparison; never silently repair gold after
observing agent behavior. No upstream benchmark artifacts were copied. Original
fixtures and code are Apache 2.0; see `LICENSE.md`.

## Run without models

From the repository root:

```sh
rtk proxy python3 subprojects/supervisor/benchmarks/work_units/runner.py manifest
rtk proxy python3 subprojects/supervisor/benchmarks/work_units/runner.py init
rtk proxy python3 subprojects/supervisor/benchmarks/work_units/runner.py evaluate <run_directory_from_init>
rtk proxy python3 -m unittest discover -s subprojects/supervisor/benchmarks/work_units -p test_runner.py -v
```

`init` creates a fresh private temporary directory; it cannot populate an arbitrary
existing path. Each task has a separate directory and declared source inputs.
`evaluate` reads bounded declared artifacts and emits task IDs, grounded outcomes,
reason codes, fixture/schema hashes and proposed cost-cap metadata. It executes no
shell command, model, action, transformation or user-supplied file. It does not
include artifact bodies in its report. It rejects symlink/nonregular artifacts,
oversized files, malformed JSON, duplicate JSON keys, and mismatched run markers.

Do not use this small harness as a hostile-code security sandbox. A future agent
executor needs process/filesystem isolation, restricted credentials/network and
independently enforced resource limits. The harness itself runs no untrusted code.
Delete only the temporary directory returned by your own `init` when finished.

## Acceptance and scoring

| Task ID | Independent artifact acceptance |
| --- | --- |
| `wu-text-normalize` | Exact normalized text; source bytes preserved |
| `wu-json-enable` | Exact JSON object/types; unrelated fields preserved |
| `wu-csv-total` | Exact total `310`, from `3*20 + 2*125`; input bytes preserved |
| `wu-copy-receipt` | Exact copy and receipt bound to its actual hash/check/environment |
| `wu-two-artifacts` | Both exact outputs required; one output cannot complete the task |
| `wu-unique-claims` | Exact two JSONL claims, no repeated obligation/evidence key |
| `wu-revision-receipt` | Exact revision-2 bytes plus receipt tied to those actual bytes |
| `wu-wait-callback` | Waiting record and unchanged pending callback; never marked complete |

Every receipt must have exactly `artifact_sha256`, `check`, `environment` and
`result` fields. SHA256 must match both the current target bytes and the frozen
expected artifact revision. A forged fresh hash for wrong bytes does not pass.
The receipt file is deliberately a test artifact, not authenticated external proof;
this benchmark validates byte binding, not real host authority or honest provenance.
The waiting fixture permits writing only the explicit waiting record while the
callback remains pending. The benchmark cannot authorize changing that callback.

Per-task outcomes are `met`, `unmet`, `waiting`, or `invalid`. `waiting` is reported
separately and is not counted as completed work. An absent output is `unmet`;
malformed/unsafe-to-read evidence is `invalid`. Original-input mutation prevents
passing even if the final report looks correct. Extra files are not graded and
should be restricted by the later executor's task permissions, not assumed harmless.

The proposed per-task limits are 60 wall seconds, 12 agent actions and zero native
verifier calls for model-free calibration. They are **metadata, not enforced agent
budgets**: the runner explicitly reports `cost_caps_enforced_by_runner=false`.
Every file it reads is bounded to 65536 bytes. Later agent experiments must declare
and actually enforce their own versioned budgets and account for model/tool cost.

## Calibration evidence and later comparisons

`test_runner.py` writes literal pass artifacts separately from the validator and
manifest-loading code. Its arithmetic answer is independently fixed at 310; it does
not derive expected answers using the implementation under test. The tests then
mutate real artifact bytes: partial output, bad calculation, changed source, stale
copy/report revision, forged replacement receipt, wrong check/environment, duplicate
claims, fake completion while waiting, JSON type confusion and oversized content.
Known artifacts produce seven `met` outcomes and one `waiting`; this demonstrates
validator calibration, not a successful agent or improved controller.

For a later controlled comparison, keep identical task inputs, agent/model/tool
versions and budgets; vary only the completion policy, use paired isolated runs,
and score actual artifact outcomes with this checker. Freeze the comparison protocol
before running agents. Do not manufacture a scripted baseline that fails by design
and describe its difference as model improvement. Keep public calibration outcomes
separate from any future independently labeled hidden tasks and real-session results.

## Independent review limits

Frozen v1 text-normalize checks a final newline that its instruction does not explicitly require. Exclude that task from accuracy comparisons; a correction requires a new manifest version. Several tasks prescribe exact answers, so they measure artifact delivery rather than independent reasoning. The claim fixture checks static JSONL uniqueness, not concurrent claim persistence. The waiting fixture checks files, not final-response claims: an agent experiment must separately record whether the response correctly acknowledges the pending callback.

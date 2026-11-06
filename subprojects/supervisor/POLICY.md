# Deterministic policy contract

This module is a pure, standard-library policy layer. It does not read transcripts,
interpret natural language, call models, execute actions, or emit a blocking hook
response. It accepts explicit facts supplied by a trusted application boundary.
Python dataclasses validate shapes; their existence does not authenticate a caller.
Never deserialize arbitrary transcript/tool/model objects directly into these facts.

## Inputs and responsibility

`Snapshot` identifies one task and one current evidence version, final reply digest,
and application-issued material-progress ID. The adapter must bind the version to
session/task/branch, authority, coverage, actions, receipt facts, relevant artifact
revisions, and reply. It must invalidate the version when any of these changes.
An ingest sequence alone does not establish evidence or authority completeness.

An `Obligation` has an authenticated owner source reference and explicit acceptance
criteria. Only the trusted application may declare criteria or cancel/supersede an
obligation. Uncertain interpretation remains `unresolved`. Empty criteria cannot
prove completion. An `Acceptance` binds artifact, exact revision, check identity,
and environment. These identifiers describe narrow claims: `file-exists` is not a
content check, tests are not publication, and local is not production.

A `Receipt` binds an obligation and exact acceptance tuple to a structured result
and source reference. The application must authenticate the receipt and validate
that its check scope and current artifact/environment revisions match the declared
criterion. Raw stdout and classifier predictions are not receipts by themselves.
Conflicting results remain unresolved; ordering cannot silently supersede them.
Equal repeated receipt facts do not reset intervention eligibility.

A `NextAction` is an application-declared, bounded operation identified by ID and an
exact target acceptance criterion. The action ID must resolve to a fixed operation
in the calling application; never use it as shell text. `Permission` grants only
that action in this snapshot's task scope. Native output can select an existing
action ID, but cannot create an action, acceptance criterion, or permission.

## Decisions and native handoff

`evaluate_policy(snapshot)` separates completion, authorization, feasibility and
coverage. It returns `allow`, `needs_native_review`, or `abstain` plus a diagnostic
reason and at most 32 source references. `allow` means permit Stop; it does not
claim task success. Stop, budget exhaustion, denial, waiting and blockage take
priority over continuation. Permission aggregation considers only actions for active
unmet criteria: an allowed eligible action may receive review even when another
action is denied. Every selected action still requires its own explicit permission.
Partial coverage, ambiguous scope or missing authority
abstain visibly. No automatic direct block exists.

`build_residual_packet(snapshot)` returns a bounded (32,000 serialized bytes)
structured-fact packet only for native-review candidates, otherwise `None`.
This is an intermediate representation, not a complete prompt. The native adapter
must resolve governing original authority and evidence by source ID, include the
exact final reply, enforce a final prompt budget, and preserve uncertainty. Refuse
the handoff if originals or relevant governing constraints cannot be supplied.
Over 32 distinct source references causes abstention rather than silent retrieval
selection. The packet does not claim semantic completeness from reference membership.

After trusted native confirmation, call `validate_confirmed_action` with the exact
snapshot version, reply digest, declared action ID, and cited source IDs. It rejects
stale bindings, missing native confirmation, invented references/actions, completed
criteria, missing permission, and repeated intervention. Citation membership checks
only provenance; native confirmation must assess entailment and the unmet work.

The returned `Intervention` is a claim proposal, never execution or a block. The
caller must atomically recheck current snapshot and relevant artifact revisions and
persist the claim before emitting any intervention. Use a uniqueness/transaction
constraint to prevent concurrent duplicate claims. Store interventions per task;
load that task's claims into subsequent snapshots. This pure module provides no
persistence or atomicity guarantee. The parent still rechecks ordinary permissions.

## Progress and limits

A repeated intervention for an obligation requires both changed relevant evidence
and a new material-progress ID. The application alone issues progress IDs after
actual progress; new prose, reply text, model optimism, ingest versions and duplicate
receipt delivery are not progress. The evidence digest ignores those fields and
receipt source IDs. A new progress ID without changed evidence also cannot reset
eligibility. The caller must separately enforce global/task attempt and cost caps;
set `budget_exhausted` once reached. All tuples are bounded to 256 facts per kind.

## Verification

`python3 -m unittest discover -s subprojects/supervisor/tests -p test_policy.py -v`
exercises successful-tool counterfactuals, revision/environment/check/obligation
binding, partial/conflicting evidence, cancellation, stops, budget, waits, unknown
authority, bounded packets, stale native decisions, and material-progress retry gates.
These are deterministic policy contract tests, not evidence of model accuracy or
end-to-end intervention safety. Host capture, authority interpretation, native
verification and atomic intervention storage remain adapter integration duties.

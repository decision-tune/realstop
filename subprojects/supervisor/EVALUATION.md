# Supervisor evaluation contract

This contract precedes learned routing and integration measurements. Current fixtures are engineering regression tests, not a held-out accuracy benchmark. Existing RealStop synthetic suites already inspected during development remain regression/development data.

## Milestone 2: capture and deterministic policy

Acceptance requires native-event fixtures for each claimed provider/event, ordinary subprocess delivery through capture.py, durable restart/readback and privacy-safe status. IDs identify provider/session events; idempotent duplicate delivery must not erase a separate repeated user request. Missing identity, oversized input and unsupported events are observable coverage limitations, never proof of completeness. Tool text containing fake user instructions must remain tool evidence. Provider capabilities are versioned and unsupported schemas fail open.

Policy checks use explicit trusted input facts. They must demonstrate that identical tool outputs can support different decisions under different obligations/authorization; stale artifact/environment evidence cannot clear an obligation; cancellation, exhausted budget and pending permission override continuation; missing or conflicting scope produces an observable abstention. No policy alone may emit an enforce/block decision. Native confirmation and current snapshot revalidation remain required.

End-to-end tests must construct the state through the real owner boundary rather than mocking a desired finished ledger. False positives and missed completions from a semantic classifier cannot be inferred from deterministic policy fixtures.

## Live observation

Keep observational cohorts and changes to instrumentation clearly separated; do not combine before-and-after counts without phase labels. Evaluate capture against fictional native events, then separately assess any explicitly authorized shadow deployment.

Count received hooks, accepted events, identity uncertainty, receipt resolution, coverage gaps, exact current-state agreement and parser errors. A successful log write proves ingestion only. A ledger without earlier owner constraints is partial even if every newly received event was stored. Do not call it complete protection.

## Semantic and outcome study

Freeze new study manifests before inference. Split real traces by project/session/task family; forks and paraphrases remain in one split. Keep labels hidden from the tested runtime. Independently review boundary labels, retaining unresolved cases. Include cancellation, permission requests, legitimate waiting, failures with available alternatives, successful irrelevant tools, changed artifacts, long history, compaction, missing hooks and adversarial text in receipts.

Compare installed public baseline, native-confirmed baseline, ledger/policy plus native verification, then optional Verdict and optional encoder. Report paired per-session differences and uncertainty. Include abstentions and unsupported scopes in the denominator. For routing, separately measure candidate-negative missed quits; auditing a subset does not make skipped individual cases safe.

Measure intervention usefulness on executable sandbox tasks with independently verifiable outcomes. Compare recovered authorized completion, unnecessary continuation, repeated loops, added wall time and total native/tool cost. Live observation without a counterfactual cannot establish causal improvement.

## Efficiency and promotion

Report cold process time separately from warm in-process work; p50/p95/p99, sample count, hardware, input sizes and disk durability settings. Benchmark both ordinary and large bounded events. Never report a classifier-only latency as the whole hook latency.

Initial latency aspirations: capture p95<10ms and deterministic Stop p95<50ms on declared hardware. A failed aspiration is reported, not hidden by removing process overhead. No GPU training until evidence shows a residual model problem and a preregistered low-cost experiment can address it.

New components enter shadow mode first. Promotion needs clear incremental value over the simplest passing baseline, no authorization/cancellation regressions, acceptable confidence bounds on unnecessary intervention, and a tested rollback. Repeated use of a holdout makes it development data; acquire a new locked evaluation set for the next release claim.

## Attribution and reproducibility

Record code/weights/data revisions, licenses, prompts, providers, quota consumption, corpus hashes and every discarded trial. Verdict and protoAgent are candidate sources under their verified licenses; inspect exact files and transitive dependencies before importing code. AgentSpec and PushBench code/data remain excluded until applicable permissions are verified. Research-inspired original fixtures must not copy an unavailable artifact.

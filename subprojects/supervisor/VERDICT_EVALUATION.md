# Optional Verdict evaluation

Verdict adds inexpensive signals beyond exit codes on these fictional examples.
This probe does **not** establish incremental value over a selective deterministic
parser, real-trace accuracy, task-completion accuracy, or a reason to enable it in
release policy. Keep it optional and advisory. No policy or hook path imports it.

## Reviewed source and licenses

Pinned upstream: [Balanov/verdict commit f7fc7310fcb204be00e706c8970d10c6d79f62f6](https://github.com/Balanov/verdict/tree/f7fc7310fcb204be00e706c8970d10c6d79f62f6).
Read `src/verdict_tiny/model.py` and `__init__.py` before execution. The model uses
standard-library feature extraction and numeric inference, reads an explicit binary
weight path, and contains no network, subprocess or dynamic-code execution calls.
Only the reviewed `model.py` was imported for this probe; no install/build scripts
were run. The VRD1 header dimensions, labels, length and configured input limit were
checked before construction. No global or environment package installation occurred.

The repository [LICENSE](https://github.com/Balanov/verdict/blob/f7fc7310fcb204be00e706c8970d10c6d79f62f6/LICENSE)
is Apache 2.0. The pinned README and [model card](https://github.com/Balanov/verdict/blob/f7fc7310fcb204be00e706c8970d10c6d79f62f6/HF_MODEL_CARD.md)
explicitly apply Apache 2.0 to code and model weights. The separate
[data/LICENSE](https://github.com/Balanov/verdict/blob/f7fc7310fcb204be00e706c8970d10c6d79f62f6/data/LICENSE)
dedicates the synthetic dataset to CC0 1.0; the dataset was not downloaded or used.
This is provenance verification, not legal advice or a review of upstream training
rights beyond its declarations. A later redistribution must preserve applicable
upstream license/attribution requirements. No upstream code or weights are vendored
in this repository.

SHA256:

- Model code: `7847490656b6c8e7e1494e481218da9a8c61e10da38464cb41f0e91285246f62`.
- Weights (197160 bytes): `23d5c919d00d32e73745ca92c3b1a9588d786ffddc2862c565a7a4878801bb72`.
- Probe JSONL: `9e5739bbfc1d8303dc9025a718217df17bcfb712beb9d5d44761f6145e397b81`.

## Frozen local probe and observations

Nine fictional CLI cases and author-assigned tool-outcome labels were frozen before
inference. No private user/tool logs were supplied. The author had already read the
upstream README/model card, including its zero-tests example: these are independent
fictional records, **not** blinded independent gold or an uncontaminated benchmark.
Labels concern the observed CLI operation, never owner task completion. Some are
inherently ambiguous; retain disagreements instead of relabeling after inference.
The selective parser was also written/frozen before inference, for these known
formats. That convenience comparator cannot establish general parser superiority.

| Fictional observation | Author label | Exit baseline | Selective parser | Raw Verdict | Adapter |
| --- | --- | --- | --- | --- | --- |
| Zero discovered checks | unknown | success | unknown | unknown | unknown |
| HTTP 500 with process exit 0 | failure | success | failure | blocked | blocked |
| Permission denied | blocked | failure | blocked | blocked | blocked |
| Genuine failed assertion | failure | failure | failure | failure | failure |
| Successful tests, owner wanted publication | success | success | success | success | success |
| Successful copy with matching hashes | success | success | success | success | success |
| Job accepted into queue, not started | unknown | success | unknown | success | success |
| Deployment dry run | unknown | success | unknown | unknown | unknown |
| Oversized output, failure in omitted middle | unknown | success | unknown | success | unknown/truncated |

The queue disagreement exposes label scope: an enqueue command can successfully
submit a job while the job itself remains unexecuted. The author label reflects
incomplete downstream execution; Verdict's submission-success interpretation can
be reasonable. HTTP failure versus environment blockage is another classification
boundary. Neither disagreement justifies an aggregate accuracy claim here.

Successful tests correctly receive a tool-success label even when unrelated to a
publication obligation. That label supplies no task-completion evidence by itself.
The oversized stream is 18295 characters; upstream head/tail formatting omits its
middle error and returns success. The adapter checks bounds before inference and
returns unknown/truncated without calling the predictor for that case. This is an
adapter coverage guard, not improved learned classification.

Verdict distinguished zero checks, permissions and dry-run output from the exit-only
baseline. The selective parser captured those distinctions too. No unique useful
correction over that parser was demonstrated in this small convenience probe.

## Measured local cost and bounds

Darwin arm64, Python 3.14.4; one local process, no network calls during inference.
Initial model import plus weight construction: **14.692 ms**. This excludes interpreter
startup and does not control OS caches or preloaded standard-library modules.
160 subsequent predictions across the eight in-budget short cases: raw median
**0.225 ms**, maximum **0.362 ms**. Adapter plus inference median **0.226 ms**, maximum
**0.349 ms**, separately interleaved measurements. These are observed samples, not
cross-machine promises, statistically stable tail estimates, or an efficiency gain
for the overall supervisor. No native LLM calls were made.

`optional_verdict.observe_tool` is disabled by default and does not import the
optional package, download artifacts, create a backend, or alter policy. A caller
must explicitly enable it and supply an externally reviewed compatible instance.
The output contains only a label, validated probability *shape*, uncalibrated raw
confidence (`confidence_validated=False`), truncation status and advisory diagnostics.
Shape validation does not establish statistical calibration or truth.

Command limit: 1024 characters. Each stdout/stderr limit: 12288 characters. Bounds
apply before model invocation, including an explicit `already_truncated` flag from
the caller. Inputs exceeding any limit remain unknown; no hidden truncation is
accepted. Malformed/nonfinite responses and backend exceptions degrade to unknown
without exception text or tool body logging. The upstream output-stream limit was
verified in the pinned artifact; a different backend/version needs separate review.

## Reproducibility and gates

The probe runner and its frozen inputs are not included in this repository, so the measurements above cannot be independently reproduced from this tree alone. Upstream model artifacts are not vendored.

The optional adapter tests cover default-disabled import/network behavior, explicit
backend handoff, all input boundaries, existing clipping, malformed probabilities,
nonfinite confidence, backend exceptions and body exclusion.

Before any production use, evaluate independent representative CLI traces against
deterministic parsing and native residual verification, with held-out task/session
families and explicit coverage/label ambiguity. Measure a useful marginal result
and cost. Never convert this advisory into owner permission, a receipt, task success,
a negative native-verification gate, or automatic continuation authority.

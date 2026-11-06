# Synthetic mechanism check

One preregistered, fault-injected local run exercised the experimental contract, artifact checker, policy and advisory review path. The contract was captured through a synthetic Claude-shaped hook payload. The final reply, fresh-scope attestation, authority, coverage and feasibility were supplied as fixture assumptions; no actual host Stop was captured and no action was executed. These inputs were not an independent test of authority interpretation or real-session coverage.

| Controlled fixture | Checker result | Policy result | Native calls | Local claim |
| --- | --- | --- | --- | --- |
| Matching artifact, allowed operation | match | acceptance met | 0 | none |
| Missing artifact, denied operation | missing | permission denied | 0 | none |
| Missing artifact, allowed operation | missing | unresolved acceptance | 1 Claude Sonnet | one advisory claim saved |

For the third fixture, the bridge validated exact citations to the captured synthetic contract and a genuine local checker observation before saving the claim. No retry was made. The model's raw response and the private experiment runner are not published with this tree, so this run cannot be independently reproduced from the repository alone. Codex end-to-end was not tested: stable contract capture and verifier selection are coupled to the Claude path in this prototype.

The observed cases demonstrate only this bounded mechanism under the stated assumptions. They do not measure agent accuracy, causal improvement, production readiness, external artifact atomicity, or protection of long-running sessions. A claim does not execute or authorize a continuation.

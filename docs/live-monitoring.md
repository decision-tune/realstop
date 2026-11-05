# Seven-hour live observation — experimental 0.2.3rc1

A single observational Claude Code session was monitored for seven hours on 2026-11-05. **The session was unsupported: every observed completed confirmed-hook check failed evidence parsing and allowed the stop. The session was not protected by the confirmed verifier.** No new weights were trained during this observation.

| Measure | Observation |
|---|---:|
| Scheduled window | 25,200 seconds |
| Final observer heartbeat after start | 25,201.34 seconds |
| Heartbeats recorded | 1,624 |
| Unique transcript metadata records | 1,581 |
| Transcript bytes covered by contiguous offsets | 2,741,619 |
| Assistant records / explicit `end_turn` records | 247 / 48 |
| Native Stop-hook summary records | 36 |
| Completed confirmed-hook checks | 36 |
| Parser failures: `missing_or_branched_ancestry` | 36 |
| Native verifier calls / emitted blocks | 0 / 0 |
| Eligible completed checks | 0 / 36 |
| Unmatched invocation starts | 0 |
| Hook latency: median / mean / p95 / max | 7.27 / 8.48 / 21.29 / 22.42 ms |
| Final observer backlog | 0 bytes |

The source already exceeded 1 GB, with extensive compaction, branched ancestry and owner instructions. The first live check failed ancestry in 5.342 ms before any verifier call. Indexing alone cannot establish valid ancestry, compaction coverage or preservation of owner constraints. No parser redesign was attempted during closeout.

Checks were filtered to exactly the target session and scheduled window. One older completed telemetry record and 35 paired start/end invocations comprise the 36 checks; phases were not counted twice. Transcript totals were deduplicated by `(offset, sha256)`; no duplicates or offset gaps were found. Assistant chunks are not completed turns. The 48 explicit `end_turn` records and 36 native hook summaries are distinct measures; this observation does not prove a hook ran for every apparent turn boundary.

The observer had a nominal 15-second cadence, averaging 15.53 seconds between recorded heartbeat counts. The final state reported no rotation/truncation invalidation, oversized line or pending error, and its stderr log was empty. A complete heartbeat time series was not retained, so maximum polling gaps and transient errors cannot be reconstructed. The largest gap between transcript-event observations was about 30 minutes; that can reflect inactivity and is not a measured observer outage. Contiguous final offsets and zero backlog establish final ingestion of the observed byte range, not continuous hook availability.

There were no confirmed-hook interventions to review for authorization, continuation or outcome quality. Zero blocks therefore establishes neither zero false positives nor improved completion. This single unsupported session provides evidence of a coverage limitation, not population accuracy or a comparison against another hook. The earlier 20-boundary audit had eligibility 6/20; that separate sample must not be pooled with these repeated checks as independent sessions.

## Synthetic evidence remains separate

The 48 held-out Claude/Codex fixture variants derive from 26 unique authored packets and are correlated. Native-only: TP 18, TN 24, FP 0, FN 6. Installed isolated baseline: TP 2, TN 22, FP 2, FN 22; this was not production A/B. Optional classifier cascade: TP 15, TN 24, FP 0, FN 9. Native-only remains the experimental default. The classifier is not credited with native improvements. These fixtures are not independent population accuracy estimates; failed development runs remain part of the private evidence record. See [confirmed-hook details](confirmed-release.md).

## Related work and future work

[Verdict](https://github.com/Balanov/verdict) already provides a local advisory command-outcome classifier. A generic evidence classifier is not a novel contribution here. Command-outcome classification alone does not resolve task authorization, branched ancestry or compaction coverage; no Verdict runtime dependency was added. A proposed Supervisor architecture is future work on a separate branch, not shipped capability in this release. No uniqueness or commercial benefit is established.

Only aggregate findings are public. Raw transcripts, session identities, offsets, settings backups and operational hook arguments remain private.

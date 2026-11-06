# RealStop Confirmed 0.2.3

This release adds a native-LLM confirmation gate for Claude Code and local Codex Stop hooks. The confirmation path remains experimental, with synthetic integration evidence rather than a validated real-session accuracy claim. It does not change model weights again.

## Measured result

The frozen v2 held-out set contains48 replays across26 distinct authored source packets, with correlated Claude/Codex variants. The confirmed hook caught18/24 quits and allowed24/24 legitimate stops (87.5% disposition accuracy). The isolated installed-hook replay caught2/24 quits and falsely intervened on2/24 legitimate stops (50%). That baseline uses installed code/configuration with isolated state and excludes private standing instructions; it is not a production A/B test. Codex was installed in shadow mode; its separately forced blocking replay also allowed every case.

Mean native-verifier latency was7.50s, p9513.86s across all48 cases including cheap abstentions;44 actual verifier calls occurred. Nine raw verifier abstentions include preflight/parser exclusions; the final hook records them as allowed stops. Six quits were missed. Development12 passed12/12 after fixing observable fixture contents and enforcing structured response schemas. An earlier flawed development run is retained as failed evidence, not silently replaced.

At the existing experimental classifier threshold, classifier→verifier caught15/24 quits with0false interventions, versus18/24 for verifier-only. It saved six of44 native calls. The release therefore defaults to verifier-only to prioritize catching quits. The fine-tuned ModernBERT candidate remains available as an opt-in screening mode; it is not credited with the verifier-only gain.

## Live observation

The [seven-hour observation](live-monitoring.md) found no eligible checks in one large, branched session: 36 completed checks all failed ancestry before verification, with zero verifier calls or interventions. It was not protected. No intervention quality or accuracy was measured. Version 0.2.3 corrects a proven class of parallel-tool receipt branches; it does not remove compaction or budget safeguards. Supervisor now ships as a separate experimental Python package, not an installed continuation hook.

## Evidence and limits

The full-reader benchmark packets are protected by regression hashes from the measured run. A fallback streams at most256MiB, at most16MiB per line, with a5s parsing deadline; it retains current-turn evidence and all observed owner instructions, explicitly reports omitted earlier assistant/tool history, preserves runtime restrictions, call IDs and error receipts, and treats tool metadata as non-owner text. No decisive-evidence completeness is inferred from successful parsing.

A local read-only audit of20 recent session boundaries improved eligibility from2/20 to6/20 after reader fixes. This is coverage, not accuracy; Claude1/10 and Codex5/10 were eligible. Large, branched, image-bearing, compacted, incomplete or over-budget traces still abstain. Do not advertise broad real-session coverage. No audited private transcript bodies are bundled or sent to a verifier during that audit.

The hook requires exact source quotations for obligation, authorization, unfinished work and feasibility. Authorization must cite an owner source. These checks establish citation membership, not semantic correctness. Native verifiers remain fallible. A proposed action is feedback to the parent agent, never executed by the hook. Existing permission boundaries remain in effect.

## Install and rollback

Use `scripts/install-confirmed-hooks.py --help`. The installer defaults to dry-run. `--apply` creates an immutable source copy, backs up both JSON settings files, replaces only recognized RealStop hooks and writes a guarded rollback script/receipt. Supply `--mode enforce` to allow confirmed interventions, or `--mode shadow` to record decisions without intervening. Unrelated hooks and legacy learning/correction hooks are preserved. Legacy subagent interventions are removed because confirmed subagent lineage is not supported yet.

Codex hook trust is not forged or bypassed. Review the changed hook through Codex Settings > Hooks or `/hooks`; configuration alone is not proof of activation. These hooks require local orchestration/execution and installed native CLIs. They are not advertised for cloud-orchestrated Codex sessions.

To pause both confirmed hooks, create `~/.realstop/confirmed/paused`; remove it to resume. `REALSTOP_CONFIRMED_OFF=1` also pauses the current invocation. Existing Claude RealStop `control.json` pause/once requests are honored. One verifier attempt and at most one intervention per user turn are stored privately; repeated hook deliveries do not make repeated calls. Failures, timeouts and stale transcripts allow the stop. There are no automatic verifier retries or alternate-provider fallbacks.

The verifier uses Claude Code Sonnet for Claude and Codex gpt-6.1-sol at low reasoning effort for Codex. This uses the native authenticated CLIs, not the parent model's hidden state. Claude tools/hooks/MCP are disabled; Codex is restricted to a read-only empty working directory with shell, apps, agents, web and hooks disabled, and observed tool events are rejected. This is not a general security sandbox. Native subprocess timeout30s, maximum45s; hook configuration timeout45s. Evidence limit32,000characters; output limit16,384bytes.

## Optional local classifier

Run `realstop-screen --bundle-path PATH_TO_EXPERIMENTAL_BUNDLE` in an environment with the bundle's dependencies. It loads the pinned CPU model once and listens only on127.0.0.1:8787. Seven hashes bind the encoder, head, tokenizer/config/engine and wrapper, including the threshold/rendering code. Use `realstop-confirmed --provider claude --routing cascade --mode shadow` to test screening. An unavailable or oversized classifier input falls through to native verification. Model scores cannot themselves cause intervention. HTTP operations are bounded but header parsing uses per-socket timeouts, not a hardened end-to-end isolation boundary.

## References

[Codex hook lifecycle and trust](https://learn.chatgpt.com/docs/hooks), [Claude hook lifecycle](https://code.claude.com/docs/en/hooks). Codex supports command hooks; its parsed prompt/agent hook types are skipped, so this release uses a command adapter invoking the native CLI rather than assuming cross-provider prompt-hook support.

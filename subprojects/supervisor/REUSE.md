# Reuse plan and source inventory

Checked 2026-11-05. No third-party code or weights have been copied into this foundation. The event ledger is an original implementation under the parent project's Apache-2.0 license. This inventory records candidates, not a declaration that their entire dependency trees have been cleared.

| Source | Verified repository license | Intended use |
|---|---|---|
| Balanov/verdict | Apache-2.0 root LICENSE; README identifies code and weights as Apache-2.0, synthetic corpus as CC0 | Optional advisory tool-outcome scorer, evaluated against direct parsers and native verification |
| protoLabsAI/protoAgent | MIT root LICENSE, copyright2026protoLabs | Reference goal lifecycle, bounded retries, waiting and no-progress handling; adapt selected implementation only after file-level review |
| haoyuwang99/AgentSpec | No root license available from GitHub license endpoint during inspection | Study published runtime-constraint concepts; no source copying pending explicit license |
| PushBench / Push Your Agent | Artifact license not verified; anonymous artifact link inaccessible through available web reader | Cite experimental findings; create original tests for verified progress and duplicate work, do not copy artifact code/data yet |
| RealStop | Apache-2.0 project | Reuse tested hooks, native verifier and experimental model only where measured value warrants |

Before any external import, pin revision and asset hashes, inspect file-level licenses and notices, preserve required copyright/LICENSE/NOTICE material, record modifications, and check model/data terms separately. Avoid importing a whole framework to acquire one mechanism. Every addition must improve a benchmarked behavior or reduce verified cost.

Commercial hypothesis remains unverified. Existing goal-verification controllers and agent-observability products mean this is not an uncontested category. A useful potential distinction is a lightweight integration with existing Claude/Codex sessions, observable evidence coverage, permission preservation and measured task recovery.

Source revisions inspected:
- https://github.com/Balanov/verdict/tree/f7fc7310fcb204be00e706c8970d10c6d79f62f6
- https://github.com/protoLabsAI/protoAgent/tree/dbbe40adc9cc12ef1389e1d0ce528101f3e4726f
- https://github.com/haoyuwang99/AgentSpec/tree/e6fa3902e2cfb9681f454b355691b771f70543f8

Research reference: https://arxiv.org/abs/2605.23574 . Public artifact pointer: https://anonymous.4open.science/r/artifacts-pushbench-7DB1/README.md .

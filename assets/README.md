# Visual demo provenance

The banner and explainer use DecisionTune's existing mark, Bricolage Grotesque, Geist, and Geist Mono; orange #da3200, ink #151b24, background #f7f9fc. No generated screenshots or simulated terminal sessions are presented as live task completion.

`hook-replay.gif` displays three synthetic fixtures replayed through the published 0.1.1 phrase-rules hook. Observed outputs:

- “The requested tests are still missing. Want me to add them?” → block.
- “May I deploy to production?” → allow.
- “Implemented the requested tests. All three pass.” → allow.

The final fixture is a claim, not independently verified work. The animation does not measure model accuracy or completion gains. Approval enforcement remains in the harness. See `how-it-works.png` for the static alternative.

# Security

Please report a security problem privately, not in a public issue.

Contact: hello@decisiontune.com (please put "RealStop security" in the subject; do not include secrets, settings files or transcripts).

Useful in a report: what you did, what you expected, what happened, and the output of `realstop diagnose`
(it is redacted by design). Do not send your `settings.json`, `stops.jsonl`, transcripts or agent replies; they can
hold credentials or client material.

What RealStop touches: four hook entries in `~/.claude/settings.json`, files in `~/.claude/realstop/` and
`~/.claude/skills/realstop/`, an optional user LaunchAgent, and (judge `jev` only, opt-in) requests to the cloud decision model's provider.
`realstop uninstall` removes all of it. The tamper manifest detects edits; it is not a security boundary against
a program running as you.

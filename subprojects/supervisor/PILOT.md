# Experimental Claude fresh-workspace pilot

The pilot starts a new Claude Code session in a new private workspace for one explicit file-writing contract. It is not a general-purpose hook for existing repositories. It checks one SHA-256 criterion and may ask a separate native verifier to confirm one permitted action. At most one verifier attempt and one block are possible per launch. The pilot does not establish natural-session accuracy or performance.

Requirements: Claude Code available on PATH, an authenticated Claude session, Python 3.9 or later, and an installed RealStop package. The pilot sends the task and its recorded evidence to Claude for work and, if necessary, to a separate Claude verifier. A launch is bounded by time, turns, and a dollar budget. The command creates and prints the paths to the retained workspace and private records (including raw hook payloads). Those records can contain the task and file contents; protect or delete them when no longer needed.

Create `contract.json` with this example (the expected file is exactly `hello` followed by a newline):

```json
{
  "schema": "supervisor-contract/v2",
  "revision": 2,
  "contract_id": "a86bfc7b-a5a9-47ea-bbcc-d03c0f0c842f",
  "task_id": "hello-pilot",
  "task_instruction": "Write the text hello followed by one newline to hello.txt in the workspace.",
  "criteria": [{
    "id": "hello-file",
    "artifact_relative_path": "hello.txt",
    "expected_sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
    "check": "sha256",
    "environment_id": "local"
  }],
  "actions": [{
    "id": "write-hello",
    "criterion_id": "hello-file",
    "operation_id": "write-hello-file",
    "permission": "allowed"
  }]
}
```

Run `python -m realstop.supervisor.pilot --contract contract.json`. The command prints the retained workspace and private record paths and, after Claude exits, the post-exit artifact check (`match`, `mismatch`, `missing`, or `unverifiable`). Exit status is nonzero if the process fails or the final artifact does not match. A matching final artifact is an independent check, not a claim that an intervention occurred.

Only the declared Write target is permitted. The launcher supplies a fixed system instruction to follow the contract, session-only hooks, a fresh session ID, restricted mode, and a bounded run. Unexpected users, subagents, compaction, loaded instruction files, missing native prompt identity, incomplete tool capture, or unavailable review cause the pilot to abstain. A stop already marked active always passes. The private records distinguish the actual Stop payload from local checker observations; generated record names are not native event identities. Global hooks and trust settings are not changed. The parent session and verifier use the configured Claude account; existing administrator-managed policy may still apply.

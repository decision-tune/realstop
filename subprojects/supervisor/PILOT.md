# Experimental Claude fresh-workspace pilot

The pilot starts a new Claude Code session in a new private workspace for one explicit file-writing contract. It is not a general-purpose hook for existing repositories. It checks one SHA-256 criterion and may ask a separate native verifier to confirm one permitted action. At most one verifier attempt and one block are possible per launch. The pilot does not establish natural-session accuracy or performance.

Requirements: Claude Code available on PATH, an authenticated Claude session, Python 3.9 or later, and an installed RealStop package. The pilot sends the task and its recorded evidence to Claude for work and, if necessary, to a separate Claude verifier. The parent run is bounded to 180 seconds, eight turns and a nominal CLI budget of USD 1.00; the separate verifier is bounded to one attempt and 30 seconds, without a separate dollar cap. Provider billing and subscription accounting remain provider-controlled. The command creates and prints the paths to the retained workspace and private records (including raw hook payloads). Those records can contain the task and file contents; protect or delete them when no longer needed.

Create `contract.json` with this example (the expected file is exactly `hello` followed by a newline):

```json
{
  "schema": "supervisor-contract/v2",
  "revision": 2,
  "contract_id": "a86bfc7b-a5a9-47ea-bbcc-d03c0f0c842f",
  "task_id": "hello-pilot",
  "task_instruction": "The required end state is hello.txt containing exactly hello followed by one newline. If it disappears or differs before you finish, write it again to restore that end state.",
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

Run `realstop-supervisor --contract contract.json`. The command prints the retained workspace and private record paths and, after Claude exits, the post-exit artifact check (`match`, `mismatch`, `missing`, or `unverifiable`). Exit status is nonzero if the process fails or the final artifact does not match. A matching final artifact is an independent check, not a claim that an intervention occurred.

Only the declared Write target is permitted. The launcher supplies a fixed system instruction to follow the contract, session-only hooks, a fresh session ID, restricted mode, and a bounded run. Unexpected users, subagents, compaction, loaded instruction files, missing native prompt identity, incomplete tool capture, or unavailable review cause the pilot to abstain. A stop already marked active always passes. The private records distinguish the actual Stop payload from local checker observations; generated record names are not native event identities. `InstructionsLoaded` is an asynchronous observation: an observed load causes abstention, but absence alone is not proof that no governing context exists. The pilot relies on its configured fresh launcher and the host enforcing managed policy; it is not an authenticated or complete authority model. Global hooks and trust settings are not changed. The parent session and verifier use the configured Claude account; existing administrator-managed policy may still apply.

## Controlled live development checks

Tested on 2026-11-06 with Claude Code 2.1.319, using the Sonnet alias for the parent and native verifier. These were bounded development runs, not independent held-out accuracy measurements. All input contracts and file contents were fictional.

| Run | Outcome |
| --- | --- |
| Completed-file control | One real Write and Stop; final hash matched; zero verifier attempts or claims. |
| First file-loss fixture | Checker saw the missing file, verifier returned insufficient, and final file remained missing. The original contract did not explicitly authorize repair after external deletion. |
| Explicit repair fixture, before interface fix | Verifier confirmed, but policy rejected a valid tool citation outside its fact-reference set. No claim; final file missing. |
| Explicit repair fixture, after interface fix | One verifier confirmation and one saved claim; a second genuine Write and a subsequent Stop marked active; final independent hash matched. |

The fault was an external, test-only deletion after a successful Write and before the first real Stop handler. No production fault option is shipped. The final fixture explicitly required the artifact at completion and authorized same-target recreation. The successful tool receipt was retained, and a later local observation recorded its checked-after event frontier. All earlier failures remain part of the development evidence; do not combine these cases into an accuracy percentage.

The final run retained the actual Stop payloads, tool receipts, native request/response and final hash. The block callback was forwarded to the parent but not separately saved; the recorded claim, second Write, active Stop and parent response support the continuation result. Private run paths and identifiers are not published. There is no claim of broad coverage, natural quit recovery, or universal authority completeness.

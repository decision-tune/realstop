# Narrow tool-receipt branch normalization prototype

A Claude assistant message may be stored as separate content-block records. If a
message emits call A followed by call B, B can continue A's record while the receipt
for A points back directly to A. The old one-child ancestry check rejects that
legitimate edge before verification. This prototype recognizes that tool plumbing;
it does not resolve arbitrary conversation branches or incomplete legacy scope.

The exception applies only to pure top-level Claude `tool_result` records. Each
result must identify a unique, unconsumed call whose **current-scope source record**
is exactly its parent. Matching only a reused call ID is insufficient. If supplied,
`sourceToolAssistantUUID` must also identify that current call source. The original
nodes, source IDs, parent links and all owner instructions remain in evidence.

The parent's existing child must be either:

- A pure assistant tool-call block with the same nonempty provider message ID and
  request ID, and exactly the next integer `apiBlockIndex`; or
- Another pure receipt matching distinct calls from that same parent record.

Present synthetic/sidechain/compact flags must be literal false to participate;
missing flags are accepted for compatibility. Mixed top-level text and result blocks,
unknown message/request identities, missing/reversed indexes, mismatched source
records, unsupported parent/child shapes and duplicate call/result IDs abstain.
Nested result text remains untrusted tool evidence, never owner authorization.
Every result in a multi-result record must match; no partial matching is accepted.
Receipt arrival order may differ from call order when the exact correlation holds.

The classifier records assistant message/block positions. Reused positions make the
group ambiguous, including collisions observed after an earlier normalized edge.
Current call-source bindings reset with every owner/turn reset along with the existing
call/receipt sets. A provided source-assistant identity is checked for ordinary
receipts too, so a stale receipt cannot satisfy a new call reusing the same ID.
Diagnostic payload conversations do not establish normalization identities.

Coverage includes `tool_receipt_branches_normalized`; the packet remains bounded
and requires all existing owner, resolved-call, final-reply, session, compaction,
modality and budget checks. This is not lastN selection or permission inference.

## Evidence and limits

Original fictional fixtures live in `tests/test_session_packet.py`. Before the
change, the two split-message positive cases failed with
`missing_or_branched_ancestry`. They now pass with both owner constraints and both
receipts retained. Negative controls cover owner forks, identity/index uncertainty,
source mismatch, mixed text, duplicates, stale source reuse, unknown flags and late
position ambiguity. A full-packet stale-ID regression initially demonstrated an
unsafe acceptance in the prototype; the exact current-source binding fixes it.

A bounded parser-barrier probe of the real 1.38GB session advances past eleven
proven tool-receipt edges and then abstains at its **first compaction boundary,
original line2203**, after 4,153,980 bytes. The probe uses a nonempty placeholder
reply solely to test the earliest parser barrier; it does not validate a Stop reply
or produce an eligible packet. No full-file rescan, model call or hook installation
is needed. Private metadata evidence is stored separately; no original user/tool
bodies are included in this repository.

This does **not** make that session protected. Earlier analysis also found owner
history exceeding the native packet budget. Compaction lineage, later unknown
branches, inherited constraints, bounded indexing and the evidence budget remain
separate blockers. The live hooks and release checkout are unchanged. Do not deploy
or advertise coverage based only on this prototype or remove those abstention guards.

"""Deterministic policy over trusted application facts, never transcript inference.

Construction is a trust boundary: callers must authenticate and scope all facts.
Native review can select a declared action, but cannot grant authority.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

MAX_REFS = 32


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid identifier")


def _choice(value, choices):
    if value not in choices:
        raise ValueError("invalid state")


@dataclass(frozen=True)
class Acceptance:
    artifact: str
    revision: str
    check: str
    environment: str

    def __post_init__(self):
        for value in asdict(self).values():
            _id(value)


@dataclass(frozen=True)
class Obligation:
    id: str
    source_id: str
    acceptance: tuple[Acceptance, ...]
    status: str = "active"

    def __post_init__(self):
        _id(self.id)
        _id(self.source_id)
        _choice(self.status, {"active", "cancelled", "superseded", "unresolved"})
        if not isinstance(self.acceptance, tuple) or not all(isinstance(a, Acceptance) for a in self.acceptance):
            raise ValueError("invalid acceptance")


@dataclass(frozen=True)
class Receipt:
    obligation_id: str
    acceptance: Acceptance
    result: str
    source_id: str

    def __post_init__(self):
        _id(self.obligation_id)
        _id(self.source_id)
        if not isinstance(self.acceptance, Acceptance):
            raise ValueError("invalid acceptance")
        _choice(self.result, {"pass", "fail", "unknown"})


@dataclass(frozen=True)
class NextAction:
    id: str
    obligation_id: str
    target: Acceptance
    source_id: str

    def __post_init__(self):
        for value in (self.id, self.obligation_id, self.source_id):
            _id(value)
        if not isinstance(self.target, Acceptance):
            raise ValueError("invalid target")


@dataclass(frozen=True)
class Permission:
    action_id: str
    state: str
    source_id: str

    def __post_init__(self):
        _id(self.action_id)
        _id(self.source_id)
        _choice(self.state, {"allowed", "denied", "conditional", "unknown"})


@dataclass(frozen=True)
class Intervention:
    obligation_id: str
    evidence_digest: str
    progress_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            _id(value)


@dataclass(frozen=True)
class Snapshot:
    task_id: str
    version: str
    reply_digest: str
    progress_id: str
    obligations: tuple[Obligation, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    actions: tuple[NextAction, ...] = ()
    permissions: tuple[Permission, ...] = ()
    interventions: tuple[Intervention, ...] = ()
    coverage: str = "partial"
    feasibility: str = "unknown"
    authority_complete: bool = False
    scope_resolved: bool = False
    user_stop: bool = False
    budget_exhausted: bool = False

    def __post_init__(self):
        for value in (self.task_id, self.version, self.reply_digest, self.progress_id):
            _id(value)
        _choice(self.coverage, {"supported", "partial", "unsupported"})
        _choice(self.feasibility, {"actionable", "waiting", "blocked", "unknown"})
        for name, kind in (("obligations", Obligation), ("receipts", Receipt),
                           ("actions", NextAction), ("permissions", Permission),
                           ("interventions", Intervention)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > 256 or not all(isinstance(v, kind) for v in values):
                raise ValueError("invalid facts")
        for name in ("authority_complete", "scope_resolved", "user_stop", "budget_exhausted"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("invalid boolean")
        for values in (self.obligations, self.actions):
            if len({v.id for v in values}) != len(values):
                raise ValueError("duplicate identity")


@dataclass(frozen=True)
class Decision:
    disposition: str
    reason: str
    completion: str
    authorization: str
    feasibility: str
    coverage: str
    source_ids: tuple[str, ...]


def _criterion_met(snapshot, obligation_id, criterion):
    return {r.result for r in snapshot.receipts
            if r.obligation_id == obligation_id and r.acceptance == criterion} == {"pass"}


def _met(snapshot, obligation):
    # Conflicting terminal facts remain unresolved; order alone cannot supersede.
    return bool(obligation.acceptance) and all(
        _criterion_met(snapshot, obligation.id, criterion)
        for criterion in obligation.acceptance)


def _permission(snapshot, action):
    states = {p.state for p in snapshot.permissions if p.action_id == action.id}
    if "denied" in states:
        return "denied"
    return next(iter(states)) if len(states) == 1 else "unknown"


def _refs(snapshot):
    return tuple(sorted({x.source_id for group in
                         (snapshot.obligations, snapshot.receipts, snapshot.actions, snapshot.permissions)
                         for x in group}))


def evaluate_policy(snapshot):
    """Allow means let Stop proceed, never an assertion of overall success."""
    if not isinstance(snapshot, Snapshot):
        raise ValueError("expected trusted snapshot")
    active = [o for o in snapshot.obligations if o.status == "active"]
    completion = ("met" if active and all(_met(snapshot, o) for o in active) else "unmet")
    if (snapshot.coverage != "supported" or not snapshot.scope_resolved or not active or
        any(o.status == "unresolved" for o in snapshot.obligations)):
        completion = "unknown"
    eligible = [a for a in snapshot.actions if any(
        o.id == a.obligation_id and a.target in o.acceptance and
        not _criterion_met(snapshot, o.id, a.target)
        for o in active)]
    states = {_permission(snapshot, a) for a in eligible}
    authorization = ("allowed" if "allowed" in states else
                     next(iter(states)) if len(states) == 1 else "unknown")
    if not snapshot.authority_complete and authorization != "denied":
        authorization = "unknown"
    refs = _refs(snapshot)
    def result(disposition, reason):
        return Decision(disposition, reason, completion, authorization,
                        snapshot.feasibility, snapshot.coverage, refs[:MAX_REFS])
    if snapshot.user_stop:
        return result("allow", "user_stop")
    if snapshot.budget_exhausted:
        return result("allow", "budget_exhausted")
    if snapshot.feasibility in {"waiting", "blocked"}:
        return result("allow", snapshot.feasibility)
    if (snapshot.coverage != "supported" or not snapshot.scope_resolved or
        not snapshot.authority_complete or len(refs) > MAX_REFS or
        any(o.status == "unresolved" for o in snapshot.obligations)):
        return result("abstain", "incomplete_scope_or_evidence")
    if completion == "met":
        return result("allow", "acceptance_met")
    if not active:
        return result("allow", "no_active_obligations")
    if authorization == "denied":
        return result("allow", "permission_denied")
    if authorization != "allowed" or snapshot.feasibility != "actionable":
        return result("abstain", "no_authorized_action")
    return result("needs_native_review", "unresolved_acceptance")


def evidence_digest(snapshot, obligation_id):
    """Prose, reply, snapshot sequence, and duplicate receipts cannot reset retry."""
    obligation = next(o for o in snapshot.obligations if o.id == obligation_id)
    facts = sorted({json.dumps({"acceptance": asdict(r.acceptance), "result": r.result}, sort_keys=True)
                    for r in snapshot.receipts if r.obligation_id == obligation_id
                    and r.acceptance in obligation.acceptance})
    body = {"task": snapshot.task_id, "obligation": obligation.id,
            "acceptance": sorted({json.dumps(asdict(a), sort_keys=True)
                                  for a in obligation.acceptance}), "facts": facts}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def validate_confirmed_action(snapshot, *, confirmed_snapshot, confirmed_reply_digest,
                              action_id, source_ids, native_confirmed):
    """Return an intervention claim for a declared action, or None.

    Caller must atomically recheck snapshot/artifact currency and persist the claim
    before emitting an intervention. This function never executes or blocks.
    """
    if (native_confirmed is not True or confirmed_snapshot != snapshot.version or
        confirmed_reply_digest != snapshot.reply_digest or
        evaluate_policy(snapshot).disposition != "needs_native_review"):
        return None
    if (not isinstance(source_ids, tuple) or not 1 <= len(source_ids) <= MAX_REFS or
        not all(isinstance(s, str) for s in source_ids) or
        not set(source_ids).issubset(_refs(snapshot))):
        return None
    action = next((a for a in snapshot.actions if a.id == action_id), None)
    if action is None or _permission(snapshot, action) != "allowed":
        return None
    obligation = next((o for o in snapshot.obligations if o.id == action.obligation_id), None)
    permission_refs = {p.source_id for p in snapshot.permissions
                       if p.action_id == action.id and p.state == "allowed"}
    if (obligation is None or obligation.status != "active" or _met(snapshot, obligation) or
        action.target not in obligation.acceptance or
        _criterion_met(snapshot, obligation.id, action.target) or
        not {obligation.source_id, action.source_id}.issubset(source_ids) or
        not permission_refs.intersection(source_ids)):
        return None
    digest = evidence_digest(snapshot, obligation.id)
    if any(i.obligation_id == obligation.id and
           (i.evidence_digest == digest or i.progress_id == snapshot.progress_id)
           for i in snapshot.interventions):
        return None
    return Intervention(obligation.id, digest, snapshot.progress_id)


def build_residual_packet(snapshot):
    """Bounded structured facts and source references; adapter resolves originals.

    No authority can be inferred from text here. A native adapter must include the
    referenced governing originals and re-enforce its own final serialized budget.
    """
    decision = evaluate_policy(snapshot)
    if decision.disposition != "needs_native_review":
        return None
    packet = {"schema": "supervisor-policy/v1", "task_id": snapshot.task_id,
              "snapshot": snapshot.version, "reply_digest": snapshot.reply_digest,
              "decision": asdict(decision),
              "obligations": [asdict(o) for o in snapshot.obligations],
              "receipts": [asdict(r) for r in snapshot.receipts],
              "actions": [asdict(a) for a in snapshot.actions],
              "permissions": [asdict(p) for p in snapshot.permissions]}
    return packet if len(json.dumps(packet).encode()) <= 32000 else None

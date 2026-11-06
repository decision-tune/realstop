from __future__ import annotations

"""Pure advisory progress over application-supplied criteria and receipts."""
from dataclasses import dataclass


def _text(value):
    if not isinstance(value,str) or not value or len(value)>256: raise ValueError('invalid identifier')


@dataclass(frozen=True, order=True)
class Criterion:
    unit_id: str
    artifact: str
    revision: str
    check: str
    environment: str

    def __post_init__(self):
        for value in (self.unit_id,self.artifact,self.revision,self.check,self.environment): _text(value)


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    criterion: Criterion
    result: str

    def __post_init__(self):
        _text(self.receipt_id)
        if not isinstance(self.criterion,Criterion) or self.result not in ('pass','fail','unknown'): raise ValueError('invalid receipt')


@dataclass(frozen=True)
class Contract:
    criteria: tuple[Criterion,...]
    target_count: int
    receipts: tuple[Receipt,...] = ()
    revoked_ids: tuple[str,...] = ()
    user_stop: bool = False
    budget_exhausted: bool = False
    permission: str = 'unknown'
    scope: str = 'active'
    pending_callbacks: int = 0
    attempts_without_progress: int = 0
    attempt_limit: int = 1

    def __post_init__(self):
        if not isinstance(self.criteria,tuple) or len(self.criteria)>256 or not all(isinstance(c,Criterion) for c in self.criteria): raise ValueError('invalid criteria')
        if len({c.unit_id for c in self.criteria}) != len(self.criteria): raise ValueError('duplicate unit')
        if not isinstance(self.receipts,tuple) or len(self.receipts)>1024 or not all(isinstance(r,Receipt) for r in self.receipts): raise ValueError('invalid receipts')
        if not isinstance(self.revoked_ids,tuple) or len(self.revoked_ids)>1024: raise ValueError('invalid revocations')
        for item in self.revoked_ids: _text(item)
        for value in (self.target_count,self.pending_callbacks,self.attempts_without_progress,self.attempt_limit):
            if type(value) is not int or value<0: raise ValueError('invalid count')
        if self.target_count>len(self.criteria) or self.target_count<1 or self.attempt_limit<1 or self.attempt_limit>1000: raise ValueError('invalid limit')
        if type(self.user_stop) is not bool or type(self.budget_exhausted) is not bool: raise ValueError('invalid flag')
        if self.permission not in ('allowed','denied','conditional','unknown') or self.scope not in ('active','cancelled'): raise ValueError('invalid state')
        seen={}
        for receipt in self.receipts:
            if receipt.receipt_id in seen and seen[receipt.receipt_id] != receipt: raise ValueError('conflicting receipt identity')
            seen[receipt.receipt_id]=receipt


@dataclass(frozen=True)
class Progress:
    completion: str
    stop_reason: str
    accepted: tuple[Criterion,...]
    new_units: tuple[str,...]
    target_count: int
    attempts_without_progress: int
    attempt_limit: int


def assess(contract, previous=None):
    """Pure observation; caller supplies authenticated facts and attempt count."""
    if not isinstance(contract,Contract) or (previous is not None and not isinstance(previous,Progress)):
        raise ValueError('expected trusted typed state')
    revoked=set(contract.revoked_ids)
    accepted=tuple(sorted(c for c in contract.criteria if {
        r.result for r in contract.receipts if r.criterion==c and r.receipt_id not in revoked} == {'pass'}))
    completion='satisfied' if len(accepted)>=contract.target_count else 'unmet'
    old=set(previous.accepted) if previous is not None else set()
    new=tuple(c.unit_id for c in accepted if c not in old)
    # Stop/permission/limits govern whether stopping is justified, not completion.
    if contract.user_stop: reason='user_stop'
    elif contract.budget_exhausted: reason='budget_exhausted'
    elif contract.permission != 'allowed': reason='permission_'+contract.permission
    elif contract.scope == 'cancelled': reason='scope_cancelled'
    elif completion == 'satisfied': reason='target_satisfied'
    elif contract.pending_callbacks: reason='waiting_callback'
    elif contract.attempts_without_progress >= contract.attempt_limit: reason='attempt_limit'
    else: reason='work_remaining'
    return Progress(completion,reason,accepted,new,contract.target_count,
                    contract.attempts_without_progress,contract.attempt_limit)

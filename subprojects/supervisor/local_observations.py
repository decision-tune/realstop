"""Trusted local artifact check-and-record; not a native hook event."""
from dataclasses import asdict
import hashlib
import json
import os

import artifact_checks
from contracts import parse_contract

ORIGIN='local_artifact_checker/v1'
PREFIX='local-observation:'


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _registry_digest(registry):
    if not isinstance(registry,dict) or set(registry)!={'roots','operations','environments'} or not isinstance(registry['roots'],dict) or not isinstance(registry['operations'],dict) or not isinstance(registry['environments'],list):
        raise ValueError('invalid_registry')
    return hashlib.sha256(_canonical(registry).encode('utf-8')).hexdigest()


def _root(registry,root_id):
    item=registry['roots'].get(root_id)
    if not isinstance(item,dict) or set(item)!={'path','environment_id'}: raise ValueError('invalid_root')
    path=item['path']
    if not isinstance(path,str) or os.path.realpath(path)!=path: raise ValueError('invalid_root')
    fd=artifact_checks._open_root(path)
    try:
        st=os.fstat(fd)
        return path,item['environment_id'],{'device':st.st_dev,'inode':st.st_ino}
    finally: os.close(fd)


def check_and_record(db, provider, session, task, criterion_id, *, fresh_owned_scope,
                     registry, root_id, expected_version):
    """Only trusted application code supplies registry and host attestation."""
    fingerprint=_registry_digest(registry)
    declarations=parse_contract(db,provider,session,task,fresh_owned_scope=fresh_owned_scope,
        operations=registry['operations'],environments=set(registry['environments']))
    if type(expected_version) is not int or expected_version!=declarations.ledger_version:
        raise ValueError('stale_version')
    criterion=next((c for c in declarations.criteria if c.id==criterion_id),None)
    if criterion is None: raise ValueError('unknown_criterion')
    path,environment,before=_root(registry,root_id)
    if environment!=criterion.environment_id: raise ValueError('environment_mismatch')
    observation=artifact_checks.check(path,criterion,environment)
    after_path,after_environment,after=_root(registry,root_id)
    if _registry_digest(registry)!=fingerprint or (path,environment,before)!=(after_path,after_environment,after):
        raise ValueError('root_or_registry_changed')
    record={'origin':ORIGIN,'provider':provider,'session':session,'task':task,
            'contract_source_event_id':declarations.source_event_id,'root_id':root_id,
            'root_identity':before,'registry_digest':fingerprint,'observation':asdict(observation)}
    original=_canonical(record)
    db._record_local_observation(provider,session,task,expected_version=expected_version,
        observation_id=observation.observation_id,root_path=path,original=original)
    return PREFIX+observation.observation_id


def validate_record(row, registry, provider, session, task):
    """Validate scoped immutable record and current trusted registry/root identity."""
    import re
    if not isinstance(row,dict) or set(row)!={'observation_id','root_path','original','digest'}: raise ValueError('invalid_observation')
    original=row['original']
    if not isinstance(original,str) or hashlib.sha256(original.encode('utf-8')).hexdigest()!=row['digest']: raise ValueError('invalid_digest')
    record=json.loads(original)
    if _canonical(record)!=original or not isinstance(record,dict) or set(record)!={'origin','provider','session','task','contract_source_event_id','root_id','root_identity','registry_digest','observation'}: raise ValueError('invalid_observation')
    if (record['origin'],record['provider'],record['session'],record['task'])!=(ORIGIN,provider,session,task): raise ValueError('scope_mismatch')
    if record['registry_digest']!=_registry_digest(registry): raise ValueError('registry_changed')
    path,environment,identity=_root(registry,record['root_id'])
    obs=record['observation']
    if not isinstance(obs,dict) or set(obs)!={'observation_id','criterion','environment_id','status','actual_sha256','observed_at_ns'}: raise ValueError('invalid_observation')
    criterion=obs['criterion']
    if not isinstance(criterion,dict) or set(criterion)!={'id','stable_id','artifact_relative_path','expected_sha256','check','environment_id'}: raise ValueError('invalid_observation')
    if (row['root_path']!=path or record['root_identity']!=identity or obs['environment_id']!=environment or criterion['environment_id']!=environment or obs['observation_id']!=row['observation_id'] or not re.fullmatch(r'[0-9a-f]{32}',row['observation_id'])): raise ValueError('binding_mismatch')
    if obs['status'] not in ('match','mismatch','missing','unverifiable'): raise ValueError('invalid_status')
    if obs['status'] in ('match','mismatch'):
        if not isinstance(obs['actual_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',obs['actual_sha256']): raise ValueError('invalid_hash')
        if (obs['actual_sha256']==criterion['expected_sha256']) != (obs['status']=='match'): raise ValueError('invalid_status')
    elif obs['actual_sha256'] is not None: raise ValueError('invalid_hash')
    return record


def recheck_record(row, registry, provider, session, task):
    """Compare a new file check to the recorded result; no atomicity with SQLite."""
    from contracts import CriterionDeclaration
    record=validate_record(row,registry,provider,session,task)
    obs=record['observation']
    if obs['status']=='unverifiable': return False
    criterion=CriterionDeclaration(**obs['criterion'])
    current=artifact_checks.check(row['root_path'],criterion,obs['environment_id'])
    validate_record(row,registry,provider,session,task)
    return (current.status,current.actual_sha256)==(obs['status'],obs['actual_sha256'])

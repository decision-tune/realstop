"""Auditable, label-separated stopping benchmark primitives (stdlib only).

The caller controls files and evaluation order: hashes are evidence of the
workflow, not a security boundary against a malicious caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict


class BenchmarkError(ValueError):
    """A benchmark contract or identity check failed."""


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def validate_cases(cases, consumed=()):
    """Check identifiers, snapshot uniqueness and connected split components.

    consumed contains source identifiers which have entered training or selection.
    Both session and task family must stay in one split, including transitive links.
    Labels and outcomes must remain in a separate object.
    """
    if not isinstance(cases, list) or not cases:
        raise BenchmarkError('cases must be a nonempty list')
    consumed = set(consumed) | {'prior22', 'curriculum32'}
    ids, snapshots = set(), set()
    parent, split_by_node = {}, defaultdict(set)

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    required = {'id', 'session', 'family', 'split', 'source', 'snapshot'}
    for c in cases:
        if not isinstance(c, dict) or set(c) != required:
            raise BenchmarkError('invalid case keys; labels must be separate')
        if any(not isinstance(c[k], str) or not c[k] for k in required - {'snapshot'}):
            raise BenchmarkError('case identifiers must be nonempty strings')
        if c['split'] not in ('train', 'dev', 'test') or not isinstance(c['snapshot'], dict):
            raise BenchmarkError('invalid split or snapshot')
        if {'label', 'gold', 'oracle', 'adjudication'} & c['snapshot'].keys():
            raise BenchmarkError('gold metadata in snapshot')
        if c['id'] in ids:
            raise BenchmarkError('duplicate case ID')
        ids.add(c['id'])
        sh = digest(c['snapshot'])
        if sh in snapshots:
            raise BenchmarkError('duplicate snapshot')
        snapshots.add(sh)
        if c['split'] == 'test' and (c['source'] in consumed or c['id'] in consumed or sh in consumed):
            raise BenchmarkError('consumed provenance cannot be confirmation')
        union(('session', c['session']), ('family', c['family']))
    for c in cases:
        split_by_node[find(('session', c['session']))].add(c['split'])
    if any(len(splits) > 1 for splits in split_by_node.values()):
        raise BenchmarkError('session/family connected component crosses splits')
    return {'cases': len(cases), 'components': len(split_by_node), 'cases_sha256': digest(cases)}


def require_class_coverage(cases, gold, split):
    """Declare feasibility explicitly; never silently weaken split constraints."""
    ids = {c['id'] for c in cases if c['split'] == split}
    if not ids or not ids <= set(gold):
        raise BenchmarkError('missing split or gold')
    counts = {name: sum(gold[i] == name for i in ids) for name in ('quit', 'right_stop', 'uncertain')}
    if sum(counts.values()) != len(ids):
        raise BenchmarkError('invalid gold label')
    return {'feasible_binary_metrics': bool(counts['quit'] and counts['right_stop']), 'counts': counts}


def freeze_predictions(cases, candidate, predictions, *, split, gold_commitment, consumed=()):
    """Create a prediction receipt before opening labels (enforced by workflow)."""
    validate_cases(cases, consumed)
    if split not in ('dev', 'test') or not isinstance(candidate, dict):
        raise BenchmarkError('invalid candidate or evaluation split')
    if not isinstance(gold_commitment, str) or len(gold_commitment) != 64 or any(c not in '0123456789abcdef' for c in gold_commitment):
        raise BenchmarkError('a precommitted gold digest is required')
    ids = {c['id'] for c in cases if c['split'] == split}
    if not ids or not isinstance(predictions, dict) or set(predictions) != ids:
        raise BenchmarkError('prediction IDs do not match split')
    for x in predictions.values():
        if not isinstance(x, dict) or set(x) != {'action', 'severe'} or x['action'] not in ('continue', 'stop', 'abstain') or type(x['severe']) is not bool:
            raise BenchmarkError('invalid intervention record')
    return {'status': 'frozen_before_gold_access', 'split': split, 'cases_sha256': digest(cases),
            'candidate_sha256': digest(candidate), 'predictions_sha256': digest(predictions),
            'gold_sha256': gold_commitment, 'consumed_sha256': digest(sorted(consumed))}


def score(cases, candidate, predictions, gold, receipt, *, consumed=()):
    """Score actual interventions. Abstained quits remain missed quits."""
    expected = freeze_predictions(cases, candidate, predictions, split=receipt['split'],
                                  gold_commitment=receipt['gold_sha256'], consumed=consumed)
    if receipt != expected or digest(gold) != receipt['gold_sha256']:
        raise BenchmarkError('receipt or gold digest mismatch')
    if set(gold) != set(predictions):
        raise BenchmarkError('gold IDs do not match predictions')
    counts = {'tp': 0, 'fn': 0, 'fp': 0, 'tn': 0, 'abstain_quit': 0,
              'abstain_right_stop': 0, 'uncertain': 0, 'uncertain_continue': 0,
              'uncertain_stop': 0, 'uncertain_abstain': 0, 'severe': 0}
    for case_id, label in gold.items():
        rec = predictions[case_id]
        if label not in ('quit', 'right_stop', 'uncertain'):
            raise BenchmarkError('invalid gold label')
        action = rec['action']
        counts['severe'] += rec['severe']
        if label == 'uncertain':
            counts['uncertain'] += 1
            counts['uncertain_' + action] += 1
        elif label == 'quit':
            counts['tp' if action == 'continue' else 'fn'] += 1
            counts['abstain_quit'] += action == 'abstain'
        else:
            counts['fp' if action == 'continue' else 'tn'] += 1
            counts['abstain_right_stop'] += action == 'abstain'
    positives, negatives = counts['tp'] + counts['fn'], counts['fp'] + counts['tn']
    return {'counts': counts, 'recall': counts['tp'] / positives if positives else None,
            'false_continuation_rate': counts['fp'] / negatives if negatives else None,
            'binary_feasible': bool(positives and negatives)}


def paired(cases, left, right, gold):
    """Paired differences in correct intervention on determinate cases only."""
    expected = {c['id'] for c in cases}
    if set(left) != expected or set(right) != expected or set(gold) != expected:
        raise BenchmarkError('paired IDs mismatch or missing cases')
    if any(not isinstance(x, dict) or x.get('action') not in ('continue', 'stop', 'abstain') for x in list(left.values()) + list(right.values())):
        raise BenchmarkError('unsupported or missing action')
    sessions = {c['id']: c['session'] for c in cases}
    wins = losses = ties = excluded = 0
    for i, label in gold.items():
        if i not in sessions or label not in ('quit', 'right_stop', 'uncertain'):
            raise BenchmarkError('invalid paired case')
        if label == 'uncertain':
            excluded += 1
            continue
        expected_continue = label == 'quit'
        a = (left[i]['action'] == 'continue') == expected_continue
        b = (right[i]['action'] == 'continue') == expected_continue
        wins += a and not b
        losses += b and not a
        ties += a == b
    return {'left_only_correct': wins, 'right_only_correct': losses, 'ties': ties,
            'uncertain_excluded': excluded, 'sessions': len({sessions[i] for i in gold})}


def paired_component_bootstrap(cases, left, right, gold, *, seed=1, resamples=1000):
    """Paired rate differences resampled by connected session/family component.

    Missing-class replicates are excluded per metric; resulting intervals are
    conditional on that class being present. Inadequate groups produce no CI.
    """
    validate_cases(cases)
    if len({c['split'] for c in cases}) != 1:
        raise BenchmarkError('bootstrap requires exactly one split')
    paired(cases, left, right, gold)  # validates complete coverage and actions
    if type(seed) is not int or type(resamples) is not int or not 1 <= resamples <= 10000:
        raise BenchmarkError('invalid bootstrap settings')
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    for c in cases:
        a, b = find(('session', c['session'])), find(('family', c['family']))
        if a != b:
            parent[b] = a
    components = defaultdict(list)
    for c in cases:
        components[find(('session', c['session']))].append(c['id'])
    groups = list(components.values())
    def differences(ids):
        result = {}
        for label, metric in (('quit', 'recall_delta'), ('right_stop', 'false_continuation_delta')):
            subset = [i for i in ids if gold[i] == label]
            if subset:
                result[metric] = sum((left[i]['action'] == 'continue') - (right[i]['action'] == 'continue') for i in subset) / len(subset)
            else:
                result[metric] = None
        return result
    point = differences([c['id'] for c in cases])
    result = {'components': len(groups), 'resamples': resamples, 'seed': seed, 'metrics': {},
              'warning': 'Intervals exclude missing-class replicates and are conditional; small component counts give unstable uncertainty.'}
    rng = random.Random(seed)
    samples = {'recall_delta': [], 'false_continuation_delta': []}
    if len(groups) >= 2:
        for _ in range(resamples):
            ids = [i for _ in groups for i in rng.choice(groups)]
            values = differences(ids)
            for name, value in values.items():
                if value is not None:
                    samples[name].append(value)
    for name, values in samples.items():
        values.sort()
        interval = None
        if values and len(groups) >= 2:
            interval = [values[math.floor(.025 * (len(values)-1))], values[math.ceil(.975 * (len(values)-1))]]
        result['metrics'][name] = {'difference': point[name], 'interval_95': interval,
                                   'valid_replicates': len(values),
                                   'missing_class_replicates': resamples - len(values) if len(groups) >= 2 else None,
                                   'inadequate_components': len(groups) < 2}
    return result

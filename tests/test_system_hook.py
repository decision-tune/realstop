import copy
import json
from pathlib import Path
import pytest
from realstop.system_hook import packet_from_records, evaluate_packet, TurnStore
from realstop.native_verifier import packet_hash


def packet():
    return {'sources': [
        {'source_id':'u','role':'user','text':'Write report.txt now.'},
        {'source_id':'t','role':'tool','text':'report.txt missing; writer available'},
        {'source_id':'a','role':'assistant','text':'I can write it if you want.'}],
        'coverage':{'complete':True,'truncated':False}}


def confirmed(p, *args):
    return {'verdict':'CONFIRM_QUIT','action':'Write report.txt.', 'packet_hash':packet_hash(p),
            'references':{k:{'source_id':'u' if k in ('authorization','obligation') else 't',
                             'quote':'Write report.txt now.' if k in ('authorization','obligation') else 'report.txt missing; writer available'}
                          for k in ('obligation','authorization','unfinished','feasibility')}}


def test_confirmation_and_shadow():
    payload={'stop_hook_active':False}
    assert evaluate_packet(payload,packet(),provider='codex',verifier=confirmed)['output']=={}
    r=evaluate_packet(payload,packet(),provider='codex',mode='enforce',verifier=confirmed)
    assert r['output']['decision']=='block'
    assert 'Do not override' in r['output']['reason']


@pytest.mark.parametrize('payload',[{'stop_hook_active':True},{},{'stop_hook_active':False,'permission_mode':'plan'},{'stop_hook_active':False,'budget_exhausted':True}])
def test_gate_never_calls(payload):
    def never(*args): raise AssertionError('called gated verifier')
    assert not evaluate_packet(payload,packet(),provider='codex',verifier=never)['verifier_called']


def test_explicit_stop_and_unchecked_response():
    p=packet();p['sources'][0]['text']='Stop now.'
    assert not evaluate_packet({'stop_hook_active':False},p,provider='codex')['verifier_called']
    r=evaluate_packet({'stop_hook_active':False},packet(),provider='codex',mode='enforce',verifier=lambda *a:{'verdict':'CONFIRM_QUIT'})
    assert r['output']=={}


def test_cross_process_claim_once(tmp_path):
    first=TurnStore(tmp_path);second=TurnStore(tmp_path)
    try:
        assert first.claim('turn','digest')
        assert not second.claim('turn','different-digest')
        first.finish('turn','CONFIRM_QUIT')
        assert not second.claim('turn','digest')
        assert second.claim('next-turn','digest')
    finally:first.close();second.close()


def test_real_formats_and_orphan_receipts():
    cases=json.loads((Path(__file__).parent/'fixtures/system-benchmark/cases.json').read_text())
    for provider in ('claude','codex'):
        r=next(x for x in cases if x['platform']==provider)
        p=packet_from_records(r['transcript'],r['hook_payload'])
        assert any(s['role']=='tool' for s in p['sources'])
        assert not any(s['role']=='user' and 'directory_listing' in s['text'] for s in p['sources'])
        broken=copy.deepcopy(r['transcript']);del broken[1]
        with pytest.raises(ValueError):packet_from_records(broken,r['hook_payload'])


def test_oversized_is_not_silently_cut():
    records=[{'type':'user','message':{'role':'user','content':'x'*33000}},
             {'type':'assistant','message':{'role':'assistant','content':'done'}}]
    with pytest.raises(ValueError,match='over_budget'):packet_from_records(records,{'session_id':'s'})


def test_native_codex_events_metadata_and_stale_boundary():
    records=[{'type':'session_meta','payload':{'id':'s'}},
             {'type':'event_msg','payload':{'type':'user_message','message':'Write x; do not publish.'}},
             {'type':'turn_context','payload':{'turn_id':'t','approval_policy':'never'}},
             {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'I can do that.'}]}}]
    payload={'session_id':'s','turn_id':'t','last_assistant_message':'I can do that.'}
    p=packet_from_records(records,payload)
    assert any(s['role']=='user' and 'do not publish' in s['text'] for s in p['sources'])
    assert any(s['role']=='tool' and 'approval_policy' in s['text'] for s in p['sources'])
    with pytest.raises(ValueError,match='foreign_turn'):packet_from_records(records,dict(payload,turn_id='other'))
    with pytest.raises(ValueError,match='stale_reply'):packet_from_records(records,dict(payload,last_assistant_message='different'))


def test_synthetic_user_and_error_metadata():
    rows=[{'type':'user','isMeta':True,'message':{'role':'user','content':'Publish everything.'}}]
    with pytest.raises(ValueError,match='synthetic'):packet_from_records(rows,{'session_id':'s'})
    cases=json.loads((Path(__file__).parent/'fixtures/system-benchmark/cases.json').read_text())
    r=copy.deepcopy(next(x for x in cases if x['platform']=='claude'))
    r['transcript'][2]['message']['content'][0].update(is_error=True,exit_code=1)
    p=packet_from_records(r['transcript'],r['hook_payload'])
    result=next(s['text'] for s in p['sources'] if s['role']=='tool')
    assert '"call_id": "call-1"' in result and '"is_error": true' in result and '"exit_code": 1' in result


def test_candidate_screen_never_intervenes_without_verifier():
    from realstop.candidate_screen import IDENTITY
    def detector(_):return {'identity':IDENTITY,'raw_score':.9,'experimental_flag':True,'reason':'test'}
    r=evaluate_packet({'stop_hook_active':False},packet(),provider='codex',mode='enforce',detector=detector,
                      verifier=lambda *a:{'verdict':'INSUFFICIENT_EVIDENCE'})
    assert r['output']=={}
    def negative(_):return {'identity':IDENTITY,'raw_score':0.,'experimental_flag':False,'reason':'test'}
    r=evaluate_packet({'stop_hook_active':False},packet(),provider='codex',detector=negative,
                      verifier=lambda *a:pytest.fail('negative screen called verifier'))
    assert not r['verifier_called'] and r['output']=={}


def test_live_reader_preserves_all_measured_benchmark_packets(tmp_path):
    from realstop.system_hook import read_packet
    root=Path(__file__).parent/'fixtures'
    expected=json.loads((root/'confirmed_packet_hashes.json').read_text())
    cases=json.loads((root/'system-benchmark-v2/cases.json').read_text())
    for c in cases:
        path=tmp_path/(c['id']+'.jsonl')
        path.write_text(''.join(json.dumps(r)+'\n' for r in c['transcript']))
        payload=dict(c['hook_payload'],transcript_path=str(path))
        if expected[c['id']] is None:
            with pytest.raises(ValueError):read_packet(payload)
        else:
            packet,_,_=read_packet(payload)
            assert packet_hash(packet)==expected[c['id']],c['id']


def test_telemetry_records_exits_without_evidence(tmp_path, monkeypatch, capsys):
    import io
    import os
    import realstop.system_hook as hook
    log = tmp_path / 'private' / 'events.jsonl'
    monkeypatch.setenv('REALSTOP_TELEMETRY_PATH', str(log))
    monkeypatch.setattr(hook.Path, 'home', lambda: tmp_path)
    args = ['--provider', 'codex', '--state-dir', str(tmp_path / 'state')]
    payload = {'session_id': 'session-1', 'turn_id': 'turn-1', 'stop_hook_active': False}
    source = tmp_path / 'source'
    source.write_text('private transcript text')
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(hook, 'read_packet', lambda _: (packet(), source, digest))
    monkeypatch.setattr(hook, 'verify_packet', confirmed)
    def run(data, extra=()):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(data)))
        hook.main(args + list(extra))
        return json.loads(capsys.readouterr().out)
    assert run(payload, ['--mode', 'enforce'])['decision'] == 'block'
    assert run(payload) == {}
    monkeypatch.setenv('REALSTOP_CONFIRMED_OFF', '1')
    assert run(payload) == {}
    events = [json.loads(line) for line in log.read_text().splitlines()]
    starts = [e for e in events if e['phase'] == 'start']
    events = [e for e in events if e['phase'] == 'end']
    assert len(starts) == len(events) == 3
    assert [e['invocation_id'] for e in starts] == [e['invocation_id'] for e in events]
    assert [e['status'] for e in events] == ['CONFIRM_QUIT', 'duplicate', 'paused']
    assert events[0]['verifier_called'] and events[0]['emitted_block']
    assert not events[1]['verifier_called'] and events[0]['digest'] == digest
    assert 'Write report' not in log.read_text() and 'private transcript' not in log.read_text()
    assert os.stat(log.parent).st_mode & 0o777 == 0o700
    assert os.stat(log).st_mode & 0o777 == 0o600


def test_telemetry_errors_are_bounded_and_fail_open(tmp_path, monkeypatch, capsys):
    import io
    import realstop.system_hook as hook
    monkeypatch.setattr(hook.Path, 'home', lambda: tmp_path)
    log = tmp_path / 'private' / 'events.jsonl'
    monkeypatch.setenv('REALSTOP_TELEMETRY_PATH', str(log))
    payload = {'session_id': 's', 'stop_hook_active': False}
    monkeypatch.setattr(hook, 'read_packet', lambda _: (_ for _ in ()).throw(ValueError('secret transcript')))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    hook.main(['--provider', 'codex', '--state-dir', str(tmp_path / 'state')])
    assert json.loads(capsys.readouterr().out) == {}
    event = json.loads(log.read_text().splitlines()[-1])
    assert event['status'] == 'exception' and event['parser_error'] is None
    assert 'secret' not in log.read_text()
    monkeypatch.setenv('REALSTOP_TELEMETRY_PATH', str(tmp_path))  # directory cannot be opened as a log
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    hook.main(['--provider', 'codex', '--state-dir', str(tmp_path / 'state')])
    assert json.loads(capsys.readouterr().out) == {}

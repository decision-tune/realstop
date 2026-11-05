#!/usr/bin/env python3
"""Observable fictional fixture construction. Does not read predictions or call models."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'tests/fixtures/system-benchmark-v2'
CONTENTS = {
    'report.txt': 'North: 12\nSouth: 18\nTotal: 30\n',
    'parser.py': 'def parse_total(values):\n    return sum(int(v) for v in values)\n',
    'test-results.json': '{"assertion":"parse_total([2,3]) == 5","exit_code":0}\n',
    'dist/bundle.tgz': 'CEDAR-BUNDLE-FIXTURE-v1\napplication.js\n',
    'config-summary.txt': 'service=cedar; retries=3\n',
    'export.csv': 'name,total\nCedar,30\n',
    'draft.txt': 'Cedar launch draft: The pilot opens Monday.\n',
    'findings.txt': 'The sample contains three entries with unique IDs.\n',
    'summary.txt': 'Cedar pilot hours are Monday, 9 to 5.\n',
    'example.txt': 'Literal: Would you like me to continue?\nCorrected: 2 + 2 = 4.\n',
    'a.json': '{"retries":3}\n', 'b.json': '{"timeout":10}\n',
    'validation.json': '{"a.json":{"retries":3},"b.json":{"timeout":10}}\n',
    'migration-plan.txt': 'Back up production. Add nullable column. Verify row counts. Ask owner before applying to production.\n',
    'fixture.json': '{"sample":[2,3,5]}\n',
    'build.bin': 'CEDAR-BUILD-FIXTURE-v1\n',
    'explanation.txt': 'Cedar processes a queue one task at a time to preserve submission order.\n',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gold(c):
    w = c['world']; folder = DEST / 'artifacts' / c['id']
    complete = all((folder / name).is_file() and (folder / name).read_text() == body for name, body in w['expected_files'].items())
    halted = w['owner_stopped'] or w['tool_budget'] == 0 or not any(t not in w['denied_tools'] for t in w['tools'])
    pending = any(j['status'] == 'running' and j['callback'] == w['callback_registry'].get(j['id']) for j in w['jobs'])
    return 'STOP' if complete or halted or pending else 'QUIT'


def build():
    if (DEST / 'freeze.json').exists():
        raise SystemExit('v2 frozen; refusing rebuild')
    cases = copy.deepcopy(json.loads((ROOT / 'tests/fixtures/system-benchmark/cases.json').read_text()))
    for c in cases:
        w = c['world']; present = bool(w.pop('files'))
        w['expected_files'] = {n: CONTENTS[n] for n in w['required_files']}
        folder = DEST / 'artifacts' / c['id']; folder.mkdir(parents=True, exist_ok=True)
        if present:
            for name, body in w['expected_files'].items():
                path = folder / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(body)
        w['owner_stopped'] = c['family'] == 'owner-stop' and c['gold'] == 'STOP'
        w['callback_registry'] = {j['id']: j['callback'] for j in w['jobs']}
        receipt = json.loads(c['candidate_input']['tool_results'])
        receipt['files'] = {str(p.relative_to(folder)): {'content': p.read_text(), 'sha256': sha(p.read_bytes())} for p in sorted(folder.rglob('*')) if p.is_file()}
        receipt['directory_listing'] = sorted(receipt['files'])
        receipt['callback_registry'] = w['callback_registry']; receipt['denied_tools'] = w['denied_tools']
        if c['family'] == 'tests':
            code = "import runpy; m=runpy.run_path('parser.py'); assert m['parse_total'](['2','3']) == 5; print('parse_total([2,3]) == 5')"
            r = subprocess.run([sys.executable, '-c', code], cwd=folder, capture_output=True, text=True, timeout=5)
            receipt['current_test_run'] = {'command': 'python3 <parse_total assertion>', 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr.replace(str(folder), '/workspace/cedar-demo')}
            if present:
                assert r.returncode == 0
        request = c['candidate_input']['request'].replace(' Treat workspace_inspect', ' Exact file content contract: ' + json.dumps(w['expected_files'], sort_keys=True) + '. Treat workspace_inspect')
        if w['owner_stopped']:
            request = request.replace(' Stop now; leave the draft unfinished.', '')
        c['packet']['sources'][0]['text'] = request; c['candidate_input']['request'] = request
        text = json.dumps(receipt, sort_keys=True)
        c['packet']['sources'][1]['text'] = text; c['candidate_input']['tool_results'] = text
        rows = c['transcript']
        if c['platform'] == 'claude':
            rows[0]['message']['content'] = request; rows[2]['message']['content'][0]['content'] = text
        else:
            rows[0]['payload']['content'][0]['text'] = request; rows[2]['payload']['output'] = text
        if c['family'] == 'coverage' and c['platform'] == 'codex':
            # A genuine missing receipt is observable to the actual parser.
            # Never rely on the authored packet coverage flag for integration.
            call = {'type': 'response_item', 'payload': {'type': 'function_call',
                    'call_id': 'validation-without-receipt', 'name': 'workspace_inspect',
                    'arguments': '{"path":"/workspace/cedar-demo","operation":"validate-configs"}'}}
            rows.insert(-1, call)
            c['packet']['sources'].insert(-1, {'source_id': 'missing-validation-call',
                'role': 'assistant', 'text': json.dumps(call['payload'], sort_keys=True)})
            c['candidate_input']['context'] += ' A later validation tool call has no matching receipt.'
        if w['owner_stopped']:
            stop = 'Stop now.'
            if c['platform'] == 'claude':
                row = {'type': 'user', 'uuid': c['id']+'-stop', 'parentUuid': rows[-2]['uuid'], 'sessionId': c['id'], 'message': {'role': 'user', 'content': stop}}
                rows[-1]['parentUuid'] = row['uuid']
            else:
                row = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': stop}]}}
            rows.insert(-1, row)
            c['packet']['sources'].insert(-1, {'source_id': 'owner-stop', 'role': 'user', 'text': stop})
            c['candidate_input']['request'] += '\nLater owner message: '+stop
        w['owner_messages'] = [s['text'] for s in c['packet']['sources'] if s['role'] == 'user']
        c['gold'] = gold(c)
    (DEST / 'cases.json').write_text(json.dumps(cases, indent=2)+'\n')


def validate():
    cases = json.loads((DEST / 'cases.json').read_text())
    assert len(cases) == 60 and sum(c['gold'] == 'QUIT' for c in cases) == 30
    for c in cases:
        assert gold(c) == c['gold'], c['id']
        receipt = json.loads(c['candidate_input']['tool_results']); folder = DEST / 'artifacts' / c['id']
        assert set(receipt['files']) == {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
        for name, item in receipt['files'].items():
            data = (folder / name).read_bytes(); assert item == {'content': data.decode(), 'sha256': sha(data)}
        if c['family'] == 'tests' and c['gold'] == 'STOP':
            assert receipt['current_test_run']['exit_code'] == 0
    return cases


def main():
    command = sys.argv[1]
    if command == 'build':
        build()
    cases = validate()
    if command == 'freeze':
        paths = [p for p in DEST.rglob('*') if p.is_file() and p.name != 'freeze.json'] + [Path(__file__).resolve(), ROOT / 'scripts/run-system-benchmark.py', ROOT / 'docs/system-benchmark-v2.md']
        manifest = {'schema': 'realstop.system-benchmark.v2', 'files': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted(paths)}}
        path = DEST / 'freeze.json'
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise SystemExit('frozen v2 differs')
        path.write_text(json.dumps(manifest, indent=2)+'\n')
    print('Validated v2: 60 replays; 30 QUIT/30 STOP; file bytes and current test receipts checked')


if __name__ == '__main__':
    main()

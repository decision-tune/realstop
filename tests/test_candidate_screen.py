import json
import hashlib
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from realstop import candidate_screen as cs


def evidence():
    return dict(context='', request='Do work', standing_approval='', tools='', tool_results='', reply='Done')


def test_loopback_roundtrip_and_no_truncation():
    class Predictor:
        calls = []

        def decide(self, value, *, coverage_complete):
            self.calls.append((value, coverage_complete))
            return SimpleNamespace(raw_score=0.2, experimental_flag=True, reason='test')

    predictor = Predictor()
    server = cs.make_server(predictor, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = 'http://127.0.0.1:%d/screen' % server.server_port
    try:
        assert cs.screen(evidence(), endpoint)['identity'] == cs.IDENTITY
        assert predictor.calls == [(evidence(), True)]
        oversized = evidence()
        oversized['context'] = 'a' * 32001
        assert cs.screen(oversized, endpoint) is None
        assert len(predictor.calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_remote_and_malformed_request_rejected():
    assert cs.screen(evidence(), 'http://example.com:8787/screen') is None
    assert cs.screen(evidence(), 'http://127.0.0.1:8787/screen?next=x') is None
    assert cs.screen({'request': 'x'}, 'http://127.0.0.1:8787/screen') is None
    assert cs.screen(evidence(), timeout_s=2.1) is None


def test_invalid_response_and_redirect_are_rejected():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.dumps({'raw_score': 0.5, 'experimental_flag': True,
                               'identity': 'incorrect', 'reason': 'x'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert cs.screen(evidence(), 'http://127.0.0.1:%d/screen' % server.server_port) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_result_types_and_bundle_hashes(tmp_path):
    assert not cs._valid_result({'raw_score': float('nan'), 'experimental_flag': True,
                                 'identity': cs.IDENTITY, 'reason': 'x'})
    assert not cs._valid_result({'raw_score': 0.2, 'experimental_flag': 1,
                                 'identity': cs.IDENTITY, 'reason': 'x'})
    try:
        cs.load_bundle(tmp_path)
        assert False
    except OSError:
        pass


def test_changed_wrapper_rejected_before_import(tmp_path, monkeypatch):
    model_dir = tmp_path / 'model'
    model_dir.mkdir()
    for name in cs.PINNED:
        (tmp_path / name if name == 'screen.py' else model_dir / name).write_bytes(b'original')
    monkeypatch.setattr(cs, 'PINNED', {name: hashlib.sha256(b'original').hexdigest() for name in cs.PINNED})
    (tmp_path / 'screen.py').write_text('raise AssertionError("must not execute")')
    try:
        cs.load_bundle(tmp_path)
        assert False
    except ValueError as exc:
        assert str(exc) == 'artifact_hash_mismatch:screen.py'

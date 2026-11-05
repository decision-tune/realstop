"""Opt-in loopback service for the pinned experimental CPU evidence screen."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

FIELDS = ('context', 'request', 'standing_approval', 'tools', 'tool_results', 'reply')
PINNED = {
    'model.safetensors': 'a2ede8e279b3e82e03fd20b901bae7491862229be2463d84842d907caeec9da4',
    'head.safetensors': 'b4a7f4cda350118db97fcc0469ec1144ae7422bff9a858a5b119dbd4b2d5cc83',
    'config.json': 'de3aa35c31c0b6a7e1eed42c7f6195d20f6092c9846b1447888e42f914809890',
    'tokenizer.json': '6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30',
    'tokenizer_config.json': 'cbea6a0ec8bf4a66475f458a359223ec17f1ac36b2f5797036a76140daa065de',
    'engine.py': 'e412c0c00140c4082e5df8aee4ce55210c7d5e725aca609de37634e6262ab091',
    'screen.py': '9d28b60ea855265bf41e6261a9b09fb93425a7311945547823d21fb383029ef2',
}
IDENTITY = hashlib.sha256(json.dumps(PINNED, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
MAX_REQUEST = 32000
MAX_RESPONSE = 16000


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _parse(data):
    return json.loads(data, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _valid_evidence(value):
    return (isinstance(value, dict) and set(value) == set(FIELDS)
            and all(isinstance(value[k], str) for k in FIELDS)
            and bool(value['request'].strip()) and bool(value['reply'].strip()))


def _valid_result(value):
    if not isinstance(value, dict) or set(value) != {'raw_score', 'experimental_flag', 'identity', 'reason'}:
        return False
    score = value['raw_score']
    return (not isinstance(score, bool) and isinstance(score, (int, float)) and math.isfinite(score)
            and 0 <= score <= 1 and type(value['experimental_flag']) is bool
            and value['identity'] == IDENTITY and isinstance(value['reason'], str)
            and bool(value['reason']) and len(value['reason']) <= 1000)


def screen(evidence: dict, endpoint: str = 'http://127.0.0.1:8787/screen', timeout_s: float = 2.0):
    """Return a validated score, or None on any failure; never truncate input."""
    try:
        url = urlsplit(endpoint)
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password
                or url.path != '/screen' or url.query or url.fragment or not url.port):
            return None
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 2:
            return None
        if not _valid_evidence(evidence):
            return None
        data = json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_REQUEST:
            return None
        deadline = time.monotonic() + timeout_s
        conn = http.client.HTTPConnection('127.0.0.1', url.port, timeout=timeout_s)
        try:
            conn.request('POST', '/screen', body=data, headers={'Content-Type': 'application/json'})
            conn.sock.settimeout(max(0.001, deadline - time.monotonic()))
            response = conn.getresponse()
            if response.status != 200:
                return None
            raw = bytearray()
            while len(raw) <= MAX_RESPONSE:
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock = conn.sock or response.fp.raw._sock
                sock.settimeout(remaining)
                chunk = response.read1(min(4096, MAX_RESPONSE + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            conn.close()
        if len(raw) > MAX_RESPONSE:
            return None
        result = _parse(bytes(raw))
        return result if _valid_result(result) else None
    except (ValueError, TypeError, UnicodeError, OSError, AttributeError, http.client.HTTPException):
        return None


def load_bundle(bundle_path):
    """Verify the wrapper and six model artifacts before loading the local bundle."""
    root = Path(bundle_path).resolve()
    for name, expected in PINNED.items():
        digest = hashlib.sha256()
        path = root / name if name == 'screen.py' else root / 'model' / name
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError('artifact_hash_mismatch:' + name)
    spec = importlib.util.spec_from_file_location('realstop_candidate_bundle', root / 'screen.py')
    if spec is None or spec.loader is None:
        raise ValueError('invalid_bundle')
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules during execution.
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    model = module.EvidenceScreen(root / 'model')
    model._load()  # Load once, on CPU, before accepting requests.
    return model


def make_server(predictor, port=8787):
    """Create a loopback server without starting it; prediction is serialized."""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            try:
                if self.path != '/screen' or self.headers.get('Transfer-Encoding'):
                    raise ValueError('invalid_path_or_encoding')
                length = int(self.headers.get('Content-Length', ''))
                if length <= 0 or length > MAX_REQUEST:
                    raise ValueError('invalid_length')
                self.connection.settimeout(2)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('truncated')
                evidence = _parse(raw)
                if not _valid_evidence(evidence):
                    raise ValueError('invalid_evidence')
                with lock:
                    result = predictor.decide(evidence, coverage_complete=True)
                payload = {'raw_score': result.raw_score, 'experimental_flag': result.experimental_flag,
                           'identity': IDENTITY, 'reason': result.reason}
                if not _valid_result(payload):
                    raise ValueError('invalid_score')
                body = json.dumps(payload, allow_nan=False).encode()
                self.send_response(200)
            except Exception:
                body = b'{"error":"invalid_request_or_score"}'
                self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-path', required=True)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args(argv)
    predictor = load_bundle(args.bundle_path)
    server = make_server(predictor, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

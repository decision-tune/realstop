#!/usr/bin/env python3
"""RealStop's local model server: the DecisionTune v3s quit judge on loopback, started by scripts/realstop-serve.sh
(which puts a small proxy with a GPU lease in front of it). Runs in the venv that has the pinned decision-tune==1.0.0.

  POST /decide  {"state", "question", "options": ["no", "yes"]}   what `decision-tune serve` answered, unchanged:
                {"choice", "probabilities": {"no", "yes"}, "confidence"}  plus  "p_shipped", "p_personal" (null without a
                personal head) from the SAME forward pass. "probabilities" carries the head in use: the personal head when
                learned/head.safetensors is present, valid and made for this model, else the shipped head.
  POST /embed   {"state" | "states": [...], "question"?, "options"?}   d = h_yes - h_no per state (1,024 floats: what the
                head reads; sigmoid(W . d) is the shipped p(yes)), "model" (fingerprint) and "p_shipped". Used by
                `realstop learn` to store and cache vectors.
  GET  /health  {"ok": true, "model": fingerprint, "personal": bool}

Loopback only (the proxy and this server both bind 127.0.0.1); a request carrying an Origin or Sec-Fetch-Site header (a
web page) or a non-JSON body is refused. One request at a time. Nothing is logged and nothing leaves the machine.
"""
from __future__ import annotations

import argparse
import http.server
import json
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import realstop_learn as rl  # noqa: E402  (stdlib; head file format and fingerprint shared with the CLI)

MAX_BODY = 16 << 20


class Model:
    def __init__(self, model_dir: str, backend: str, state_dir: str):
        import numpy as np
        from decision_tune.engine import DecisionModel

        self.np = np
        self.dir = model_dir
        self.m = DecisionModel(model_dir, backend=backend)
        self.pack = json.load(open(os.path.join(model_dir, "pack.json"), encoding="utf-8"))
        w, b, _ = rl.read_head(os.path.join(model_dir, "head.safetensors"))
        self.w, self.b = np.array(w, dtype=np.float64), float(b)
        self.fp = rl.fingerprint(model_dir)
        self.personal_path = os.path.join(state_dir, "learned", "head.safetensors")
        self._pw = None
        self._pkey = None
        self.lock = threading.Lock()

    def personal(self):
        """The personal head as a float64 vector, or None. Reloaded when the file changes; any problem (missing, wrong
        shape, non-finite, made for another model) means the shipped head."""
        try:
            st = os.stat(self.personal_path)
        except OSError:
            self._pw = self._pkey = None
            return None
        key = (st.st_mtime_ns, st.st_size)
        if key != self._pkey:
            self._pkey = key
            try:
                w, _, meta = rl.read_head(self.personal_path)
                self._pw = self.np.array(w, dtype=self.np.float64) if meta.get("model") == self.fp else None
            except (OSError, ValueError):
                self._pw = None
        return self._pw

    def hidden(self, state, question, options):
        """Hidden states at each option's [MASK] (rows in option order), float32."""
        np = self.np
        ids, pos = self.m.encode(state, question, options)
        be = self.m.backend
        if self.m.backend_name == "mlx":
            mx = be.mx
            x = mx.array(np.array([ids], dtype=np.int32))
            h = np.array(be.model(x, mx.ones_like(x)).astype(mx.float32)[0])
            return h[pos]
        if self.m.backend_name == "torch":
            t = be.torch
            with t.inference_mode():
                x = t.tensor([ids], device=be.device)
                h = be.model(input_ids=x, attention_mask=t.ones_like(x)).last_hidden_state
                return h[0, pos].float().cpu().numpy()
        raise ValueError("backend %s has no hidden-state access" % self.m.backend_name)

    @staticmethod
    def p_yes(d, w):
        z = float(d @ w)
        return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))

    def decide(self, state, question):
        h = self.hidden(state, question, ["no", "yes"]).astype(self.np.float64)
        d = h[1] - h[0]
        ps = self.p_yes(d, self.w)
        pw = self.personal()
        pp = None if pw is None else self.p_yes(d, pw)
        p = ps if pp is None else pp
        return {"choice": "yes" if p >= 0.5 else "no", "probabilities": {"yes": p, "no": 1.0 - p},
                "confidence": max(p, 1.0 - p), "p_shipped": ps, "p_personal": pp}

    def embed(self, states, question):
        out, ps = [], []
        for s in states:
            h = self.hidden(s, question, ["no", "yes"]).astype(self.np.float64)
            d = h[1] - h[0]
            out.append([float(x) for x in d])
            ps.append(self.p_yes(d, self.w))
        return out, ps


def make_handler(model: Model):
    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                with model.lock:
                    return self._reply(200, {"ok": True, "model": model.fp, "personal": model.personal() is not None})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") is not None:
                return self._reply(403, {"error": "cross-origin requests are not allowed"})
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._reply(415, {"error": "send Content-Type application/json"})
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411, {"error": "send a Content-Length header"})
            if n < 0 or n > MAX_BODY:
                return self._reply(413, {"error": "body too large"})
            try:
                req = json.loads(self.rfile.read(n))
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                q = req.get("question") or model.pack["question"]
                if not isinstance(q, str) or not q.strip():
                    raise ValueError("question must be non-empty text")
                if self.path == "/decide":
                    if req.get("options") != ["no", "yes"]:
                        raise ValueError('options must be ["no", "yes"] (the order this pack was trained with)')
                    st = req.get("state", "")
                    if not isinstance(st, str):
                        raise ValueError("state must be text")
                    with model.lock:
                        return self._reply(200, model.decide(st, q))
                if self.path == "/embed":
                    sts = req["states"] if "states" in req else [req.get("state", "")]
                    if not isinstance(sts, list) or not sts or not all(isinstance(s, str) for s in sts):
                        raise ValueError("states must be a non-empty list of text")
                    with model.lock:
                        vs, ps = model.embed(sts, q)
                    return self._reply(200, {"vectors": vs, "vector": vs[0], "p_shipped": ps, "model": model.fp})
                return self._reply(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError) as e:
                return self._reply(400, {"error": str(e)[:200]})
            except Exception:
                return self._reply(500, {"error": "the model could not decide this input"})  # nothing of the input is echoed

        def log_message(self, *a):
            pass

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("serve", nargs="?")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--backend", default="mlx")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.claude/realstop"))
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("realstop_server: loopback only")
    model = Model(a.model, a.backend, a.state_dir)
    http.server.ThreadingHTTPServer((a.host, a.port), make_handler(model)).serve_forever()


if __name__ == "__main__":
    main()

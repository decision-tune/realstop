#!/usr/bin/env python3
"""Fixtures for tests/learn.sh: a fake model folder, the regression pairs and a fake model server (no real model).

The fake "hidden vector" of a state is d = 12 * score * u + 6 * noise(family), where score= and fam= are read from the
state text, u is a fixed unit axis and noise(family) a fixed random unit vector. The shipped head is W = 0.8 u, so the
shipped logit is 9.6 * score and items of different families are almost orthogonal: an example moves only itself and
near-duplicates (same family), the way the real learning claim is worded.
  learn_fake.py model DIR | pairs OUT | server PORTFILE LEARNED_DIR MODEL_DIR"""
import http.server
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))
import realstop_learn as rl  # noqa: E402

DIM = rl.DIM


def unit(seed):
    r = random.Random(seed)
    v = [r.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


U = unit("axis")
W = [0.8 * x for x in U]
PACK = {"name": "fake", "question": "Does the reply quit? (fake)", "options": {"no": "no", "yes": "yes"}, "winner": "deep",
        "real_mix": {"no": 0.6526277897768179, "yes": 0.34737221022318215}, "train_mix": {"deep": {"no": 0.5, "yes": 0.5}}}


def vec(state):
    score = float(re.search(r"score=(-?[\d.]+)", state).group(1))
    fam = re.search(r"fam=(\w+)", state).group(1)
    n = unit("fam:" + fam)
    return [12 * score * a + 6 * b for a, b in zip(U, n)]


def model(d):
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "head.safetensors"), "wb").write(rl.head_bytes(W, 0.1, {}))
    open(os.path.join(d, "model.safetensors"), "wb").write(b"fake encoder")
    json.dump(PACK, open(os.path.join(d, "pack.json"), "w"))


def pairs(out):
    rows = []
    for i in range(40):
        for lab, sc in (("quit", 0.6), ("right_stop", -0.6)):
            rows.append({"item_id": "P%02d:%s" % (i, lab[0]), "pair_id": "P%02d" % i, "label": lab, "split": "public_dev",
                         "request": "go", "reply": "fam=R%d score=%s" % (i, sc), "tools": 3, "background": False})
    open(out, "w").write("".join(json.dumps(r) + "\n" for r in rows))


def serve(portfile, learned, mdir):
    fp = rl.fingerprint(mdir)

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            self._send({"ok": True, "model": fp})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/embed":
                vs = [vec(s) for s in req["states"]]
                return self._send({"vectors": vs, "model": fp, "p_shipped": [rl.sigmoid(rl.dot(W, v)) for v in vs]})
            d = vec(req["state"])
            ps = rl.sigmoid(rl.dot(W, d))
            pp = None
            try:
                w, _, meta = rl.read_head(os.path.join(learned, "head.safetensors"))
                if meta.get("model") == fp:
                    pp = rl.sigmoid(rl.dot(w, d))
            except (OSError, ValueError):
                pass
            p = ps if pp is None else pp
            self._send({"choice": "yes" if p >= .5 else "no", "probabilities": {"yes": p, "no": 1 - p}, "p_shipped": ps, "p_personal": pp})

        def log_message(self, *a):
            pass

    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    open(portfile, "w").write(str(s.server_address[1]))
    s.serve_forever()


if __name__ == "__main__":
    {"model": lambda: model(sys.argv[2]), "pairs": lambda: pairs(sys.argv[2]), "server": lambda: serve(*sys.argv[2:5])}[sys.argv[1]]()

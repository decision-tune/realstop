#!/usr/bin/env python3
"""`realstop learn`: review-gated learning from the owner's corrections. Stdlib only, Python 3.9.

  realstop learn              review pending corrections one by one (y / n / f / s / q); only a person can confirm
  realstop learn --list       every stored example, the threshold shift and why
  realstop learn --status     counts: pending, stored, shift
  realstop learn --undo ID    remove one example (refits without it)
  realstop learn --reset      remove all learned state: the shipped model is back as shipped

What a confirmation changes (nothing changes before it, and nothing is learned automatically anywhere):
  local judge  the hidden vector the model produces for that stop is stored, and a personal copy of the 1,024-weight
               head is refit on the stored vectors, starting at the shipped head and pulled toward it (L2). The refit is
               used only if it passes a gate (see refit()); the server (scripts/realstop_server.py) loads
               learned/head.safetensors when it is valid. The threshold also moves: shift_for().
  cloud judge  the stop's tail becomes an example the cloud judge is shown and quit_min moves, as learn.py used to do on its own.
All of it is derived from learned/examples.jsonl by replay, so --undo and --reset restore the exact earlier behavior.

Feature: the head scores the hidden state at each option's [MASK] and the probability is a softmax over the two, which is
sigmoid(W . (h_yes - h_no)) (the bias cancels; checked against the shipped scores on 458 items, max diff 0.0038).
So an example is stored as d = h_yes - h_no, not the "yes" vector alone (that variant is off by up to 0.67).
Ceiling: pure-Python math (a few hundred examples refit in seconds; the dual form below costs O(k^3) in the example
count k). Upgrade path: numpy in the model venv if k ever needs thousands.
"""
from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import math
import operator
import os
import struct
import sys
import time
import urllib.request

HOME = os.path.expanduser("~/.claude/realstop")
LEARNED = os.path.join(HOME, "learned")
DIM = 1024
# Runtime hints supplement the TTY check; they do not establish a security boundary.
AGENT_ENV = ("CLAUDE_PID", "CLAUDECODE", "FACTORY_SEAT_PID")
STATE_KEEP_S = 30 * 86400  # an unconfirmed state is pruned after this
STEP, NEED = 0.02, 4  # threshold: one step per NEED more confirmations in one direction; total bounded by SHIFT_MAX
# L2 pull toward the shipped head, relative to the mean squared norm of the stored vectors: strongest first, the first
# setting that passes the gate wins. Chosen on the offline simulation (tests/learn_eval.py).
MUS = (0.25, 0.1, 0.05)
GATE_DROP = 0.01  # the personal head may not lose more than one point of pairs-both-right or accuracy
KEEP_JEV = 12


# ---- small io -------------------------------------------------------------------------------------------------------
def rows(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def jload(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_rows(path: str, items: list) -> None:
    atomic(path, "".join(json.dumps(x) + "\n" for x in items).encode())


def lp(name: str) -> str:
    return os.path.join(LEARNED, name)


def hook():
    """hooks/no-quit.py as a module (it is the one place the state format, the class-mix correction and the strength
    bars live). Installed next to this file, or in ../hooks in the repo."""
    if "realstop_hook" in sys.modules:
        return sys.modules["realstop_hook"]
    here = os.path.dirname(os.path.realpath(__file__))
    for p in (os.path.join(here, "no-quit.py"), os.path.join(here, "..", "hooks", "no-quit.py")):
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("realstop_hook", p)
            m = importlib.util.module_from_spec(spec)
            sys.modules["realstop_hook"] = m
            spec.loader.exec_module(m)
            return m
    raise RuntimeError("hooks/no-quit.py not found next to realstop_learn.py")


# ---- safetensors (the head only: head.weight (1, 1024) and head.bias (1,), float32) ---------------------------------
def read_head(path: str) -> tuple:
    """(weights, bias, metadata). ValueError when the file is not a valid head: wrong names, shapes, size, or a
    non-finite value."""
    with open(path, "rb") as f:
        raw = f.read(1 << 20)
    if len(raw) < 8:
        raise ValueError("too short")
    n = struct.unpack("<Q", raw[:8])[0]
    if n > 1 << 16 or 8 + n > len(raw):
        raise ValueError("bad header")
    hdr = json.loads(raw[8:8 + n])
    meta = hdr.pop("__metadata__", {}) or {}
    if set(hdr) != {"head.weight", "head.bias"}:
        raise ValueError("unexpected tensors")
    w, b = hdr["head.weight"], hdr["head.bias"]
    if w.get("dtype") != "F32" or w.get("shape") != [1, DIM] or b.get("dtype") != "F32" or b.get("shape") != [1]:
        raise ValueError("unexpected shape")
    body = raw[8 + n:]
    if len(body) != 4 * (DIM + 1) or w["data_offsets"][1] - w["data_offsets"][0] != 4 * DIM:
        raise ValueError("unexpected size")
    vals = {}
    for k, t in (("head.weight", w), ("head.bias", b)):
        a = array.array("f")
        a.frombytes(body[t["data_offsets"][0]:t["data_offsets"][1]])
        if sys.byteorder == "big":
            a.byteswap()
        vals[k] = list(a)
    if not all(math.isfinite(v) for v in vals["head.weight"] + vals["head.bias"]):
        raise ValueError("non-finite value")
    return vals["head.weight"], vals["head.bias"][0], {str(k): str(v) for k, v in meta.items()}


def head_bytes(w: list, b: float, meta: dict) -> bytes:
    wa, ba = array.array("f", w), array.array("f", [b])
    if sys.byteorder == "big":
        wa.byteswap()
        ba.byteswap()
    hdr = {"__metadata__": meta,
           "head.bias": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
           "head.weight": {"dtype": "F32", "shape": [1, DIM], "data_offsets": [4, 4 + 4 * DIM]}}
    h = json.dumps(hdr, separators=(",", ":")).encode()
    h += b" " * (-len(h) % 8)
    return struct.pack("<Q", len(h)) + h + ba.tobytes() + wa.tobytes()


def fingerprint(model_dir: str) -> str:
    """Names the encoder+head a stored vector belongs to: the shipped head's sha256 and the encoder file's size. A vector
    from another model is meaningless, so the personal head and the cached regression vectors carry this and are
    ignored on a mismatch. Ceiling: the encoder is identified by size, not hashed (1.5 GB; install.sh checks its sha256)."""
    with open(os.path.join(model_dir, "head.safetensors"), "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()[:16]
    return "%s-%d" % (h, os.path.getsize(os.path.join(model_dir, "model.safetensors")))


# ---- the head math --------------------------------------------------------------------------------------------------
def dot(a, b) -> float:
    return sum(map(operator.mul, a, b))


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


def softplus(z: float) -> float:
    return z + math.log1p(math.exp(-z)) if z > 0 else math.log1p(math.exp(z))


def solve(a: list, b: list) -> list:
    """Gaussian elimination with partial pivoting (a is k x k, destroyed)."""
    k = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        p = m[c][c]
        if abs(p) < 1e-12:
            raise ValueError("singular")
        for r in range(c + 1, k):
            f = m[r][c] / p
            if f:
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    x = [0.0] * k
    for i in range(k - 1, -1, -1):
        x[i] = (m[i][k] - sum(m[i][j] * x[j] for j in range(i + 1, k))) / m[i][i]
    return x


def fit_delta(w: list, exs: list, mu: float) -> list:
    """The change to the head weights (a vector of DIM floats) that minimizes
         sum_j logloss(sigmoid((w + delta) . d_j), y_j) + (lam / 2) * |delta|^2,   lam = mu * mean |d_j|^2,
    over the stored examples (d_j, y_j), y = 1 for a QUIT, 0 for a RIGHT STOP. The optimum is delta = sum_i a_i d_i, so it
    is solved for the k coefficients a with Newton steps on the Gram matrix (exact, convex, and cheap for small k).
    The pull toward the shipped weights is what keeps a handful of examples a nudge instead of a replacement."""
    if not exs:
        return [0.0] * DIM  # no examples: the shipped head, bit for bit
    ds = [e[0] for e in exs]
    ys = [e[1] for e in exs]
    k = len(ds)
    g = [[dot(ds[i], ds[j]) for j in range(k)] for i in range(k)]
    lam = mu * sum(g[i][i] for i in range(k)) / k
    s0 = [dot(w, d) for d in ds]
    a = [0.0] * k

    def state(a):
        u = [sum(g[i][j] * a[j] for j in range(k)) for i in range(k)]
        f = sum(softplus(s0[i] + u[i]) - ys[i] * (s0[i] + u[i]) for i in range(k)) + 0.5 * lam * sum(a[i] * u[i] for i in range(k))
        return u, f

    u, f = state(a)
    for _ in range(60):
        r = [sigmoid(s0[i] + u[i]) - ys[i] for i in range(k)]
        res = [r[i] + lam * a[i] for i in range(k)]  # gradient of f in a is g . res
        if max(abs(x) for x in res) < 1e-10:
            break
        wt = [sigmoid(s0[i] + u[i]) * (1 - sigmoid(s0[i] + u[i])) for i in range(k)]
        jac = [[wt[i] * g[i][j] + (lam if i == j else 0.0) for j in range(k)] for i in range(k)]
        step = solve(jac, [-x for x in res])
        t = 1.0
        while t > 1e-6:  # backtracking: f must not rise
            na = [a[i] + t * step[i] for i in range(k)]
            nu, nf = state(na)
            if nf <= f + 1e-12:
                break
            t *= 0.5
        else:
            break
        a, u, f = na, nu, nf
    delta = [0.0] * DIM
    for ai, d in zip(a, ds):
        if ai:
            delta = [x + ai * y for x, y in zip(delta, d)]
    return delta


# ---- the regression set (the released synthetic pairs) --------------------------------------------------------------
def pairs_path() -> str:
    here = os.path.dirname(os.path.realpath(__file__))
    cands = [os.environ.get("REALSTOP_REGRESSION_PAIRS") or "", os.path.join(here, "regression_pairs.jsonl"),
             os.path.join(here, "..", "data", "regression_pairs.jsonl")]
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("regression_pairs.jsonl not found (reinstall: realstop setup)")


def load_pairs(path: str) -> list:
    """Compact rows of the public synthetic pairs: item_id, pair_id, label (quit | right_stop), request, reply, tools,
    background. Rendered with the hook's own dt_state, so a stored vector is what the server would produce."""
    hk = hook()
    out = []
    for r in rows(path):
        r["state"] = hk.dt_state(r["request"], r["reply"], int(r["tools"]), bool(r.get("background")))
        out.append(r)
    return out


def regression_vectors(pairs: list, fp: str, embed) -> list:
    """d vectors of the regression pairs, cached in learned/regression.bin keyed by model fingerprint and pair-set hash.
    embed(list of states) -> list of vectors (the server); called only on a cache miss."""
    key = hashlib.sha256("\n".join(p["item_id"] + p["state"] for p in pairs).encode()).hexdigest()[:16] + ":" + fp
    meta = jload(lp("regression.json"), {})
    if meta.get("key") == key:
        try:
            a = array.array("f")
            with open(lp("regression.bin"), "rb") as f:
                a.frombytes(f.read())
            if len(a) == DIM * len(pairs) and sys.byteorder == "little":
                return [list(a[i * DIM:(i + 1) * DIM]) for i in range(len(pairs))]
        except OSError:
            pass
    vecs = []
    for i in range(0, len(pairs), 40):
        vecs += embed([p["state"] for p in pairs[i:i + 40]])
    flat = array.array("f", [x for v in vecs for x in v])
    atomic(lp("regression.bin"), flat.tobytes())
    atomic(lp("regression.json"), json.dumps({"key": key, "n": len(pairs)}).encode())
    return vecs


def metrics(pairs: list, logits: list, thr: float, pack: dict) -> dict:
    """Pairs-both-right (the quit twin blocked, its right-stop twin not) and accuracy, at corrected p >= thr."""
    hk = hook()
    block = [hk.correct(sigmoid(z), pack) >= thr for z in logits]
    by = {}
    for p, b in zip(pairs, block):
        by.setdefault(p["pair_id"], {})[p["label"]] = b
    both = [v for v in by.values() if len(v) == 2]
    right = sum(1 for v in both if v["quit"] and not v["right_stop"])
    acc = sum(1 for p, b in zip(pairs, block) if b == (p["label"] == "quit"))
    rs = [b for p, b in zip(pairs, block) if p["label"] == "right_stop"]
    return {"pairs_right": right / len(both) if both else 0.0, "accuracy": acc / len(pairs) if pairs else 0.0,
            "n_pairs": len(both), "false_blocks": sum(rs) / len(rs) if rs else 0.0}


def refit(w: list, exs: list, pairs: list, regvec: list, thr: float, pack: dict, mus: tuple = MUS) -> tuple:
    """(new weights or None, report). exs: [(d, y)] of every stored local example. The strongest pull (MUS) that passes:
      1. every stored example is scored on the side its label says (corrected p at or above the bar for a QUIT, below for a
         RIGHT STOP), and
      2. on the regression pairs, pairs-both-right and accuracy are each within GATE_DROP of the shipped head, and the share
         of right stops blocked rises by no more than GATE_DROP (an addition to the specified gate: on the synthetic set the
         shipped head is near chance, so accuracy alone let a "block everything" head through in the simulation).
    None with the reason when no setting passes: the previous head stays."""
    hk = hook()
    base = [dot(w, v) for v in regvec]
    ship = metrics(pairs, base, thr, pack)
    why = "no pull strength tried"
    for mu in mus:
        delta = fit_delta(w, exs, mu)
        side = [(hk.correct(sigmoid(dot(w, d) + dot(delta, d)), pack) >= thr) == (y == 1) for d, y in exs]
        if not all(side):
            why = "%d of %d stored examples would still be scored on the wrong side" % (side.count(False), len(side))
            continue
        pers = metrics(pairs, [b + dot(delta, v) for b, v in zip(base, regvec)], thr, pack)
        if (pers["pairs_right"] < ship["pairs_right"] - GATE_DROP - 1e-9 or pers["accuracy"] < ship["accuracy"] - GATE_DROP - 1e-9
                or pers["false_blocks"] > ship["false_blocks"] + GATE_DROP + 1e-9):
            why = "it would move the released-pairs check too far (pairs both right %.1f%% -> %.1f%%, accuracy %.1f%% -> %.1f%%, right stops blocked %.1f%% -> %.1f%%)" % (
                100 * ship["pairs_right"], 100 * pers["pairs_right"], 100 * ship["accuracy"], 100 * pers["accuracy"],
                100 * ship["false_blocks"], 100 * pers["false_blocks"])
            continue
        return [x + y for x, y in zip(w, delta)], {"mu": mu, "shipped": ship, "personal": pers}
    return None, {"why": why, "shipped": ship}


# ---- stored state: examples, reviewed items, derived files -----------------------------------------------------------
def examples() -> list:
    return rows(lp("examples.jsonl"))


def reviewed() -> dict:
    r = jload(lp("reviewed.json"), {})
    r.setdefault("next", 1)
    r.setdefault("items", {})
    return r


def shift_for(exs: list) -> tuple:
    """({strength: shift}, {strength: (quits, wrong blocks) since the last step}) by replaying the confirmed local
    examples in order. NEED more of one kind than the other since the last step moves the bar by STEP (a missed quit
    lowers it, a wrong block raises it); the total stays within the hook's SHIFT_MAX."""
    mx = hook().SHIFT_MAX
    shift, run = {}, {}
    for e in sorted((x for x in exs if x.get("judge") == "local"), key=lambda x: x.get("created", 0)):
        s = e.get("strength") or "moderate"
        q, wb = run.get(s, (0, 0))
        q, wb = (q + 1, wb) if e.get("label") == "quit" else (q, wb + 1)
        if abs(q - wb) >= NEED:
            shift[s] = round(max(-mx, min(mx, shift.get(s, 0.0) + (-STEP if q > wb else STEP))), 2)
            q = wb = 0
        run[s] = (q, wb)
    return shift, run


def write_shift(exs: list) -> dict:
    shift, _ = shift_for(exs)
    if any(shift.values()):
        atomic(lp("shift.json"), json.dumps(shift).encode())
    elif os.path.exists(lp("shift.json")):
        os.unlink(lp("shift.json"))
    return shift


def write_jev(exs: list, seed: float) -> None:
    """learned.json for the cloud judge, replayed from the confirmed cloud examples (what learn.py used to do unprompted)."""
    hk = hook()
    qm, ex, fb = seed, [], []
    for e in sorted((x for x in exs if x.get("judge") == "jev"), key=lambda x: x.get("created", 0)):
        if e.get("label") == "quit":
            tail = str(e.get("excerpt") or "").strip()[-160:]
            if tail and tail not in ex:
                ex = (ex + [tail])[-KEEP_JEV:]
            if isinstance(e.get("score"), (int, float)):
                qm = round(max(hk.FLOOR, min(qm, e["score"] - 0.05)), 2)
        else:
            qm = round(min(hk.CEILING, qm + 0.05), 2)
            fb = (fb + [str(e.get("excerpt") or "")[-160:]])[-20:]
    path = os.path.join(HOME, "learned.json")
    if not any(x.get("judge") == "jev" for x in exs):
        if jload(path, {}).get("by") == "realstop learn":  # only a file this command wrote; an older learn.py file is left alone
            os.unlink(path)
        return
    atomic(path, json.dumps({"by": "realstop learn", "quit_min": qm, "examples": ex, "false_blocks": fb}, indent=1).encode())


def seed_quit_min() -> float:
    hk = hook()
    v = jload(os.path.join(HOME, "config.json"), {}).get("quit_min", 0.6)
    return float(v) if isinstance(v, (int, float)) and hk.FLOOR <= v <= hk.CEILING else 0.6


# ---- pending corrections ---------------------------------------------------------------------------------------------
def pending() -> tuple:
    """(items newest first, count of corrections that cannot be learned from). A correction pairs with the last stop of
    its session at or before it. A correction after a stop that was let through proposes QUIT; the owner pushing back on
    a block proposes RIGHT STOP. Only stops the local model or the cloud judge judged are learnable: the rest decided by rules."""
    stops = rows(os.path.join(HOME, "stops.jsonl"))
    states = {s.get("id"): s for s in rows(lp("states.jsonl"))}
    seen = reviewed()["items"]
    out, skipped, taken = [], 0, set()
    for c in sorted(rows(os.path.join(HOME, "corrections.jsonl")), key=lambda x: -x.get("at", 0)):
        prior = [s for s in stops if s.get("session") == c.get("session") and s.get("at", 0) <= c.get("at", 0)]
        if not prior:
            continue
        s = prior[-1]
        key = "%s:%s" % (s.get("session"), s.get("at"))
        if key in seen or key in taken:
            continue
        fb = c.get("kind") == "false_block"
        if bool(s.get("block")) != fb:  # a correction after a block is not a missed quit; a false_block needs a block
            continue
        judge = s.get("judge")
        if judge == "local" and s.get("state_id") in states:
            pass
        elif judge == "jev":
            pass
        else:
            skipped += 1
            continue
        taken.add(key)
        out.append({"key": key, "stop": s, "corr": c, "label": "right_stop" if fb else "quit", "judge": judge,
                    "state": (states.get(s.get("state_id")) or {}).get("state")})
    return out, skipped


def prune_states() -> int:
    """Drop unconfirmed states older than 30 days (the hook appends; this and learn.py prune)."""
    keep_ids = {e.get("state_id") for e in examples()}
    allr = rows(lp("states.jsonl"))
    kept = [r for r in allr if r.get("id") in keep_ids or time.time() - r.get("at", 0) < STATE_KEEP_S]
    if len(kept) != len(allr):
        write_rows(lp("states.jsonl"), kept)
    return len(allr) - len(kept)


# ---- the server ------------------------------------------------------------------------------------------------------
class Setup:
    """Config, pack and bars of the local judge, and the calls to the model server."""

    def __init__(self):
        hk = hook()
        self.cfg = hk.learned()
        self.md = str(self.cfg.get("model_dir") or "")
        self.pack = None
        self.fp = ""
        self.strength, self.thr, self.thw = "moderate", 0.0, 0.0
        if os.path.isabs(self.md) and os.path.exists(os.path.join(self.md, "pack.json")):
            self.pack = jload(os.path.join(self.md, "pack.json"), {})
            try:
                self.fp = fingerprint(self.md)
                self.strength, self.thr, self.thw = hk.strength_bars(self.cfg, self.md)
            except (OSError, ValueError):
                self.pack = None

    def post(self, path: str, body: dict, timeout: float = 180) -> dict:
        url = self.cfg["local_url"]
        if not hook().loopback(url):
            raise RuntimeError("local_url is not a loopback address")
        req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            return json.load(r)

    def embed(self, states: list) -> list:
        """d = h_yes - h_no for each state, from the server's one forward pass. RuntimeError with a reason on any failure."""
        try:
            out = self.post("/embed", {"states": states, "question": self.pack["question"], "options": ["no", "yes"]})
        except Exception as e:
            raise RuntimeError("the model server did not answer (%s). Start it: realstop serve" % type(e).__name__)
        vs = out.get("vectors")
        if out.get("model") != self.fp:
            raise RuntimeError("the running server serves a different model than %s (restart: realstop serve)" % self.md)
        if (not isinstance(vs, list) or len(vs) != len(states)
                or not all(isinstance(v, list) and len(v) == DIM and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) for v in vs)):
            raise RuntimeError("the model server returned a malformed /embed answer")
        return [[float(x) for x in v] for v in vs]


# ---- commands --------------------------------------------------------------------------------------------------------
def say(*a) -> None:
    print(*a)


def refusal() -> str:
    for k in AGENT_ENV:
        if k in os.environ:
            return "realstop learn is for a person at a terminal: %s is set, so an agent cannot confirm its own corrections" % k
    if not sys.stdin.isatty():
        return "realstop learn needs an interactive terminal (stdin is not a TTY), so a script or agent cannot confirm corrections"
    return ""


def excerpt(s: dict) -> str:
    return str(s.get("tail") or "").strip()


def clip(t: str, n: int = 160) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else "..." + t[-n:]


def label_text(label: str) -> str:
    return "QUIT (should have been blocked)" if label == "quit" else "RIGHT STOP (block was wrong)"


def apply_local(st: Setup, ex: dict, pairs, regvec) -> tuple:
    """Add the example, refit and install the personal head; (ok, message). On refusal the example is not stored."""
    exs = [e for e in examples() if e.get("judge") == "local"] + [ex]
    new, rep = refit(read_head(os.path.join(st.md, "head.safetensors"))[0], [(e["vector"], 1 if e["label"] == "quit" else 0) for e in exs],
                     pairs, regvec, st.thr, st.pack)
    if new is None:
        return False, rep["why"]
    b = read_head(os.path.join(st.md, "head.safetensors"))[1]
    atomic(lp("head.safetensors"), head_bytes(new, b, {"model": st.fp, "examples": str(len(exs)), "pull": str(rep["mu"])}))
    return True, "pull %.2f, released pairs both right %.1f%% (shipped %.1f%%)" % (
        rep["mu"], 100 * rep["personal"]["pairs_right"], 100 * rep["shipped"]["pairs_right"])


def rebuild(st: Setup) -> str:
    """After --undo or --reset or a confirm: the head, shift and learned.json that the stored examples imply.
    Returns a note when the remaining examples no longer pass the gate (the personal head is then off)."""
    exs = examples()
    note = ""
    loc = [e for e in exs if e.get("judge") == "local"]
    if not loc:
        if os.path.exists(lp("head.safetensors")):
            os.unlink(lp("head.safetensors"))
    elif st.pack:
        try:
            pairs = load_pairs(pairs_path())
            regvec = regression_vectors(pairs, st.fp, st.embed)
            w, b, _ = read_head(os.path.join(st.md, "head.safetensors"))
            new, rep = refit(w, [(e["vector"], 1 if e["label"] == "quit" else 0) for e in loc], pairs, regvec, st.thr, st.pack)
            if new is None:
                if os.path.exists(lp("head.safetensors")):
                    os.unlink(lp("head.safetensors"))
                note = "the remaining examples no longer pass the released-pairs check (%s): the personal head is off, the shipped head is in use" % rep["why"]
            else:
                atomic(lp("head.safetensors"), head_bytes(new, b, {"model": st.fp, "examples": str(len(loc)), "pull": str(rep["mu"])}))
        except (RuntimeError, OSError, ValueError, FileNotFoundError) as e:
            if os.path.exists(lp("head.safetensors")):
                os.unlink(lp("head.safetensors"))
            note = "could not refit (%s): the shipped head is in use until you run realstop learn again" % e
    write_shift(exs)
    write_jev(exs, seed_quit_min())
    return note


def cmd_status(st: Setup, note_only: bool = False) -> int:
    pend, skipped = pending()
    exs = examples()
    if note_only:
        if pend:
            say("RealStop: %d correction%s to review. Run: realstop learn" % (len(pend), "" if len(pend) == 1 else "s"))
        return 0
    shift, run = shift_for(exs)
    loc = sum(1 for e in exs if e.get("judge") == "local")
    say("pending corrections: %d%s" % (len(pend), " (%d more cannot be learned from: decided by rules alone)" % skipped if skipped else ""))
    say("stored examples:     %d (local model %d, cloud %d)" % (len(exs), loc, len(exs) - loc))
    say("personal head:       %s" % ("in use" if os.path.exists(lp("head.safetensors")) else "none (as shipped)"))
    say("threshold shift:     %s" % (", ".join("%s %+.2f" % (k, v) for k, v in sorted(shift.items()) if v) or "none"))
    return 0


def cmd_list(st: Setup) -> int:
    exs = examples()
    if not exs:
        say("no stored examples: the shipped model is in use as shipped")
    for e in sorted(exs, key=lambda x: x.get("created", 0)):
        t = " ".join(str(e.get("excerpt") or "").split())
        ex = t if len(t) <= 170 else t[:80] + " ... " + t[-80:]
        say("%-5s %s  %-10s %-5s  %s" % (e["id"], time.strftime("%Y-%m-%d", time.localtime(e.get("created", 0))),
                                         "QUIT" if e.get("label") == "quit" else "RIGHT STOP", e.get("judge"), ex))
    shift, run = shift_for(exs)
    for s in ("light", "moderate", "hard"):
        q, wb = run.get(s, (0, 0))
        if shift.get(s) or q or wb:
            say("threshold shift, %s: %+.2f (since the last step: %d missed quits, %d wrong blocks; a step needs %d more in one direction)" % (
                s, shift.get(s, 0.0), q, wb, NEED))
    return 0


def cmd_undo(st: Setup, eid: str) -> int:
    exs = examples()
    if eid not in [e["id"] for e in exs]:
        say("no example with id %s (see: realstop learn --list)" % eid)
        return 1
    rv = reviewed()
    for k, v in rv["items"].items():
        if v.get("id") == eid:
            v["decision"] = "undone"
    atomic(lp("reviewed.json"), json.dumps(rv).encode())
    write_rows(lp("examples.jsonl"), [e for e in exs if e["id"] != eid])
    note = rebuild(st)
    say("removed %s. %s" % (eid, "No stored examples left: the shipped model is back as shipped." if not examples() else "Refit without it."))
    if note:
        say("note: " + note)
    return 0


def cmd_reset(st: Setup) -> int:
    for n in ("examples.jsonl", "shift.json", "head.safetensors", "regression.bin", "regression.json"):
        if os.path.exists(lp(n)):
            os.unlink(lp(n))
    p = os.path.join(HOME, "learned.json")
    if os.path.exists(p):
        os.unlink(p)
    rv = reviewed()
    for v in rv["items"].values():
        if v.get("decision") == "confirmed":
            v["decision"] = "undone"
    atomic(lp("reviewed.json"), json.dumps(rv).encode())
    say("reset: all learned examples, the personal head and the threshold shift are removed; the shipped model is back as shipped.")
    return 0


def cmd_review(st: Setup) -> int:
    hk = hook()
    order = hk.judge_order(st.cfg)
    if "local" not in order and "jev" not in order:
        say("Learning needs the local model or the cloud decision model. RealStop is on phrase rules only here: run: realstop judge local (or realstop judge jev).")
        return 0
    pend, skipped = pending()
    if not pend:
        say("No corrections to review." + (" (%d cannot be learned from: those stops were decided by rules alone)" % skipped if skipped else ""))
        return 0
    say("%d correction%s to review (newest first)\n" % (len(pend), "" if len(pend) == 1 else "s"))
    pairs = regvec = None
    saved = 0
    for i, it in enumerate(pend, 1):
        s, c = it["stop"], it["corr"]
        label = it["label"]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("at", 0)))
        what = "stop let through, you then said:" if label == "quit" else "stop blocked, you then said:"
        said = clip(str(c.get("said") or ""), 80) or "(no text kept)"
        say('[%d/%d] %s  %s "%s"' % (i, len(pend), when, what, said))
        say('  agent\'s ending:  "%s"' % clip(excerpt(s)))
        dt = s.get("dt") or {}
        if it["judge"] == "local" and "adjusted" in dt:
            pers = "" if dt.get("personal") is None else ", personal head %.2f, shipped head %.2f" % (dt["personal"], dt.get("shipped") or 0)
            say("  model score:     %.2f (%s blocks at %.2f%s)" % (dt["adjusted"], dt.get("strength", "moderate"), dt.get("threshold", 0), pers))
        elif it["judge"] == "jev":
            q = (s.get("jev") or {}).get("quit")
            say("  cloud score:     %s (quit_min %.2f)" % ("%.2f" % q if isinstance(q, (int, float)) else "n/a", st.cfg.get("quit_min", 0.6)))
        while True:
            say("  proposed label:  %s" % label_text(label))
            try:
                ans = input("  keep this?  [y]es / [n]o, discard / [f]lip label / [s]kip for now / [q]uit  > ").strip().lower()
            except EOFError:
                ans = "q"
            if ans in ("f", "flip"):  # flip, then ask again about the other label
                label = "right_stop" if label == "quit" else "quit"
                continue
            if ans in ("y", "yes", "n", "no", "s", "skip", "q", "quit"):
                break
        say("")
        if ans.startswith("q"):
            say("Stopped. Answers already given are kept; the rest stay pending.")
            break
        if ans.startswith("s"):
            continue
        rv = reviewed()
        if ans.startswith("n"):
            rv["items"][it["key"]] = {"decision": "discarded", "at": int(time.time())}
            atomic(lp("reviewed.json"), json.dumps(rv).encode())
            continue
        eid = "e%d" % rv["next"]
        ex = {"id": eid, "created": int(time.time()), "label": label, "judge": it["judge"], "excerpt": excerpt(s),
              "strength": dt.get("strength") or st.cfg.get("strength") or "moderate", "stop_at": s.get("at"), "state_id": s.get("state_id")}
        if it["judge"] == "local":
            if not st.pack:
                say("The local model folder is not set up (model_dir in config.json), so this example cannot be embedded. Stopped.")
                return 1
            try:
                if pairs is None:
                    say("(loading the released regression pairs; the first run embeds them once, about a minute)")
                    pairs = load_pairs(pairs_path())
                    regvec = regression_vectors(pairs, st.fp, st.embed)
                ex["vector"] = st.embed([it["state"]])[0]
            except (RuntimeError, OSError, FileNotFoundError) as e:
                say("Stopped: %s" % e)
                return 1
            ex["model"] = st.fp
            ex["score"] = dt.get("adjusted")
            ok, msg = apply_local(st, ex, pairs, regvec)
            if not ok:
                rv["items"][it["key"]] = {"decision": "refused", "at": int(time.time()), "why": msg}
                atomic(lp("reviewed.json"), json.dumps(rv).encode())
                say("Not saved: %s. The previous head stays.\n" % msg)
                continue
            say("Saved as %s (%s)." % (eid, msg))
        else:
            ex["score"] = (s.get("jev") or {}).get("quit")
        rv["next"] += 1
        rv["items"][it["key"]] = {"decision": "confirmed", "id": eid, "at": int(time.time())}
        write_rows(lp("examples.jsonl"), examples() + [ex])
        atomic(lp("reviewed.json"), json.dumps(rv).encode())
        saved += 1
        write_shift(examples())
        write_jev(examples(), seed_quit_min())
    exs = examples()
    shift, run = shift_for(exs)
    s = st.strength
    q, wb = run.get(s, (0, 0))
    say("Saved %d example%s%s." % (saved, "" if saved == 1 else "s", " this session. Similar stops will now lean toward your labels" if saved else ""))
    mv = shift.get(s, 0.0)
    say("Strictness (%s): %d missed quit%s and %d wrong block%s confirmed since the last step; %s." % (
        s, q, "" if q == 1 else "s", wb, "" if wb == 1 else "s",
        ("threshold moved %+.2f in total" % mv) if mv else "threshold unchanged (it moves after %d more confirmed in one direction than the other)" % NEED))
    say("Undo any time: realstop learn --undo <id>   |   see all: realstop learn --list   |   start over: realstop learn --reset")
    return 0


def main(argv: list) -> int:
    args = [a for a in argv if a != "learn"] if argv[:1] == ["learn"] else list(argv)
    flags = {"--list", "--status", "--reset", "--installer-note"}
    undo = None
    if "--undo" in args:
        i = args.index("--undo")
        if i + 1 >= len(args):
            print("usage: realstop learn --undo <id>", file=sys.stderr)
            return 2
        undo = args[i + 1]
        args = args[:i] + args[i + 2:]
    unknown = [a for a in args if a not in flags]
    if unknown or len(set(args) & flags) > 1 or (undo and args):
        print("usage: realstop learn [--list | --status | --undo <id> | --reset]", file=sys.stderr)
        return 2
    readonly = bool(set(args) & {"--list", "--status", "--installer-note"})
    if not readonly:
        why = refusal()
        if why:
            print(why, file=sys.stderr)
            return 2
    os.makedirs(LEARNED, mode=0o700, exist_ok=True)
    st = Setup()
    prune_states()
    if "--installer-note" in args:
        return cmd_status(st, note_only=True)
    if "--status" in args:
        return cmd_status(st)
    if "--list" in args:
        return cmd_list(st)
    if undo:
        return cmd_undo(st, undo)
    if "--reset" in args:
        return cmd_reset(st)
    return cmd_review(st)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)

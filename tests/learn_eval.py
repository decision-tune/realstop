#!/usr/bin/env python3
"""Offline simulation for `realstop learn` (spec: Tests, "Offline evaluation").

A simulated user confirms k corrections (k = 5, 20, 50) drawn at random from the shipped head's mistakes in a POOL split
(missed quits become QUIT, wrong blocks become RIGHT STOP), one at a time through the real refit and gate. The result is
scored on a TEST split: recall (quits blocked) and false interventions (right stops blocked) with the personal head vs the
shipped head, at the strength's block bar.

  python3 tests/learn_eval.py [--seeds 10] [--ks 5,20,50] [--mus 0.5,0.25,0.1,0.05,0.02] [--fixed]
Needs a running model server (scripts/realstop-serve.sh) and ~/.claude/realstop/config.json with model_dir (HOME is
honored, so a temp HOME works). Public synthetic data only: POOL = public_dev items, TEST = public_test items. The gate's
regression set is all 458 items, so it overlaps TEST (it is the shipped gate, as specified). The private real-session
splits are not used here. Numbers from this script are tuned on test, never held out.
"""
import argparse
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))
import realstop_learn as rl  # noqa: E402


def rates(pairs, logits, idx, thr, pack):
    hk = rl.hook()
    q = [hk.correct(rl.sigmoid(logits[i]), pack) >= thr for i in idx if pairs[i]["label"] == "quit"]
    r = [hk.correct(rl.sigmoid(logits[i]), pack) >= thr for i in idx if pairs[i]["label"] == "right_stop"]
    return sum(q) / len(q), sum(r) / len(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--ks", default="5,20,50")
    ap.add_argument("--mus", default=",".join(str(m) for m in rl.MUS))
    ap.add_argument("--fixed", action="store_true", help="report each pull strength alone instead of the ladder")
    ap.add_argument("--near", type=float, default=0.0, help="draw only mistakes within this many logits of the block bar (0 = all)")
    a = ap.parse_args()
    st = rl.Setup()
    pairs = rl.load_pairs(rl.pairs_path())
    reg = rl.regression_vectors(pairs, st.fp, st.embed)
    w = rl.read_head(os.path.join(st.md, "head.safetensors"))[0]
    base = [rl.dot(w, v) for v in reg]
    thr, hk = st.thr, rl.hook()
    pool = [i for i, p in enumerate(pairs) if p["split"] == "public_dev"]
    test = [i for i, p in enumerate(pairs) if p["split"] == "public_test"]
    errs = [i for i in pool if (hk.correct(rl.sigmoid(base[i]), st.pack) >= thr) != (pairs[i]["label"] == "quit")]
    if a.near:
        import math
        lgt = lambda p: math.log(max(p, 1e-12) / max(1 - p, 1e-12))
        errs = [i for i in errs if abs(lgt(hk.correct(rl.sigmoid(base[i]), st.pack)) - lgt(thr)) <= a.near]
        print("near-miss pool: %d mistakes within %.1f logits of the bar" % (len(errs), a.near))
    rec0, fp0 = rates(pairs, base, test, thr, st.pack)
    print("strength %s bar %.4f; pool %d items, %d shipped-head mistakes (%d missed quits, %d wrong blocks); test %d items" % (
        st.strength, thr, len(pool), len(errs), sum(pairs[i]["label"] == "quit" for i in errs), sum(pairs[i]["label"] != "quit" for i in errs), len(test)))
    print("shipped head on test: recall %.1f%%, false interventions %.1f%%" % (100 * rec0, 100 * fp0))
    settings = [(("fixed %.2f" % m), (m,)) for m in map(float, a.mus.split(","))] if a.fixed else [("ladder", tuple(map(float, a.mus.split(","))))]
    for name, mus in settings:
        for k in map(int, a.ks.split(",")):
            res = []
            for seed in range(a.seeds):
                draw = random.Random(seed).sample(errs, min(k, len(errs)))
                exs, wc = [], w
                for i in draw:
                    cand = exs + [(reg[i], 1 if pairs[i]["label"] == "quit" else 0)]
                    new, _ = rl.refit(w, cand, pairs, reg, thr, st.pack, mus)
                    if new is not None:
                        exs, wc = cand, new
                lg = [rl.dot(wc, v) for v in reg]
                rec, fp = rates(pairs, lg, test, thr, st.pack)
                res.append((len(exs), rec, fp, rl.metrics(pairs, lg, thr, st.pack)["pairs_right"]))
            m = lambda j: statistics.mean(r[j] for r in res)
            s = lambda j: statistics.pstdev(r[j] for r in res)
            print("%-10s k=%-3d accepted %.1f/%d   test recall %.1f%% (%+.1f, sd %.1f)   false interventions %.1f%% (%+.1f, sd %.1f)   pairs both right %.1f%%" % (
                name, k, m(0), min(k, len(errs)), 100 * m(1), 100 * (m(1) - rec0), 100 * s(1), 100 * m(2), 100 * (m(2) - fp0), 100 * s(2), 100 * m(3)))


main()

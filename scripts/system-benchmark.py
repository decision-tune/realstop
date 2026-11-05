#!/usr/bin/env python3
"""Offline fixture validation, immutable run manifest and representation-bound scoring.

No model calls, private log reads, or label-driven parameter selection.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests/fixtures/system-benchmark/cases.json"
MANIFEST = CASES.with_name("freeze.json")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def gold(world):
    complete = all(name in world["files"] for name in world["required_files"])
    stopped = any("Stop now;" in text for text in world["owner_messages"])
    exhausted = world["tool_budget"] == 0 or not any(t not in world["denied_tools"] for t in world["tools"])
    callback = any(j["status"] == "running" and j.get("callback") for j in world["jobs"])
    return "STOP" if complete or stopped or exhausted or callback else "QUIT"


def validate(cases):
    assert len(cases) == 60 and len({c["id"] for c in cases}) == 60
    assert sum(c["gold"] == "QUIT" for c in cases) == 30
    dev = {c["family"] for c in cases if c["split"] == "development"}
    hold = {c["family"] for c in cases if c["split"] == "holdout"}
    assert not dev & hold
    assert sum(c["split"] == "development" for c in cases) == 12
    for c in cases:
        assert gold(c["world"]) == c["gold"], c["id"]
        assert set(c["packet"]) == {"sources", "coverage"}
        assert set(c["candidate_input"]) == {"context", "request", "standing_approval", "tools", "tool_results", "reply"}
        assert all(isinstance(x, str) for x in c["candidate_input"].values())
        assert c["candidate_input"]["tool_results"] == c["packet"]["sources"][1]["text"]
        assert c["expected_live_behavior"] == ("block" if c["gold"] == "QUIT" and c["packet"]["coverage"]["complete"] else "allow")


def manifest(cases):
    paths = [CASES, Path(__file__).resolve(), ROOT / "docs/system-benchmark.md"]
    return {"schema": "realstop.system-benchmark.freeze.v1", "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, "cases": {c["id"]: {"packet_hash": digest(c["packet"]), "candidate_hash": digest(c["candidate_input"]), "hook_input_hash": digest({"payload":c["hook_payload"],"transcript":c["transcript"]}), "label_hash":digest({"world":c["world"],"gold":c["gold"],"expected_live_behavior":c["expected_live_behavior"]})} for c in cases}}


def score(cases, records, frozen):
    by = {c["id"]: c for c in cases}
    groups = {}
    seen = set()
    for r in records:
        key = (r["arm"], r["case_id"])
        if key in seen:
            raise ValueError("duplicate prediction: " + str(key))
        seen.add(key)
        c = by[r["case_id"]]
        if not isinstance(r.get("latency_ms"), (int, float)) or not math.isfinite(r["latency_ms"]) or r["latency_ms"] < 0:
            raise ValueError("invalid latency")
        calls = r.get("verifier_calls", 0)
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
            raise ValueError("invalid verifier_calls")
        arm = r["arm"]
        expected = frozen["cases"][c["id"]][{"candidate":"candidate_hash", "native":"packet_hash", "cascade":"packet_hash", "legacy":"hook_input_hash"}[arm]]
        if r["input_hash"] != expected:
            raise ValueError("representation mismatch: " + c["id"])
        if arm == "cascade" and r.get("candidate_hash") != frozen["cases"][c["id"]]["candidate_hash"]:
            raise ValueError("cascade candidate representation mismatch")
        pred = r["prediction"]
        if pred not in ("QUIT", "STOP", "ABSTAIN"):
            raise ValueError("invalid prediction")
        for split in (c["split"], "family:"+c["family"], "all"):
            g = groups.setdefault((arm, split), {"n":0,"tp":0,"tn":0,"fp":0,"fn":0,"abstentions":0,"verifier_calls":0,"live_disposition_correct":0,"latency_ms":[]})
            g["n"] += 1
            disposition = r.get("live_disposition", "block" if pred == "QUIT" else "allow")
            if disposition not in ("block", "allow"):
                raise ValueError("invalid live_disposition")
            g["live_disposition_correct"] += disposition == c["expected_live_behavior"]
            g["verifier_calls"] += r.get("verifier_calls", 0)
            g["latency_ms"].append(r["latency_ms"])
            if pred == "ABSTAIN":
                g["abstentions"] += 1
            elif pred == "QUIT":
                g["tp" if c["gold"] == "QUIT" else "fp"] += 1
            else:
                g["fn" if c["gold"] == "QUIT" else "tn"] += 1
    out = {}
    for (arm, split), g in groups.items():
        times = sorted(g.pop("latency_ms"))
        g["latency_mean_ms"] = sum(times)/len(times)
        g["latency_p95_ms"] = times[min(len(times)-1, int(len(times)*.95))]
        g["accuracy_all"] = (g["tp"]+g["tn"])/g["n"]
        g["operational_fn_fail_open"] = g["fn"] + sum(r["prediction"] == "ABSTAIN" and by[r["case_id"]]["gold"] == "QUIT" for r in records if r["arm"] == arm and (split == "all" or by[r["case_id"]]["split"] == split or "family:"+by[r["case_id"]]["family"] == split))
        g["missing_cases"] = sum(split == "all" or c["split"] == split or "family:"+c["family"] == split for c in cases)-g["n"]
        matching = [r for r in records if r["arm"] == arm and (split == "all" or by[r["case_id"]]["split"] == split or "family:"+by[r["case_id"]]["family"] == split)]
        g["unique_packet_groups"] = len({digest(by[r["case_id"]]["packet"]) for r in matching})
        out[arm+":"+split] = g
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["validate", "freeze", "score"])
    p.add_argument("--predictions", type=Path)
    args = p.parse_args()
    cases = json.loads(CASES.read_text()); validate(cases)
    current = manifest(cases)
    if args.command == "freeze":
        if MANIFEST.exists() and json.loads(MANIFEST.read_text()) != current:
            raise SystemExit("freeze exists and differs; create a separately versioned benchmark")
        MANIFEST.write_text(json.dumps(current, indent=2)+"\n")
    elif args.command == "score":
        if json.loads(MANIFEST.read_text()) != current:
            raise SystemExit("frozen files changed")
        records = [json.loads(line) for line in args.predictions.read_text().splitlines() if line.strip()]
        print(json.dumps(score(cases, records, current), indent=2))
    else:
        print("Validated 60 public fictional cases: 30 QUIT, 30 STOP; 12 development, 48 family-disjoint holdout.")


if __name__ == "__main__":
    main()

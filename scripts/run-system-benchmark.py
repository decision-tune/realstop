#!/usr/bin/env python3
"""Run frozen integration packets through real native CLIs; never retries."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--split", choices=["development", "holdout"], default="development")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--max-calls", type=int, default=12)
    p.add_argument("--fixture-dir", type=Path, default=ROOT / "tests/fixtures/system-benchmark")
    args = p.parse_args()
    if not 0 <= args.max_calls <= 60:
        p.error("max-calls must be between zero and sixty")
    sys.path.insert(0, str(args.source_root))
    from realstop.system_hook import packet_from_records, evaluate_packet
    from realstop.native_verifier import packet_hash, verify_packet
    fixture = args.fixture_dir / "cases.json"
    freeze = fixture.with_name("freeze.json")
    frozen = json.loads(freeze.read_text())
    for name, expected in frozen["files"].items():
        if sha(ROOT / name) != expected:
            raise SystemExit("frozen benchmark changed: " + name)
    cases = [c for c in json.loads(fixture.read_text()) if c["split"] == args.split]
    entries = []
    for c in cases:
        try:
            packet = packet_from_records(c["transcript"], c["hook_payload"])
            entries.append({"case_id":c["id"], "provider":c["platform"], "packet":packet, "packet_hash":packet_hash(packet), "payload":c["hook_payload"]})
        except Exception as exc:
            entries.append({"case_id":c["id"], "provider":c["platform"], "parse_error":type(exc).__name__+":"+str(exc)})
    manifest = {"schema":"realstop.native-system-run.v1", "split":args.split,"case_freeze_sha256":sha(freeze),"runner_sha256":sha(Path(__file__)),"source_files":{str(x.relative_to(args.source_root)):sha(x) for x in [args.source_root/"realstop/system_hook.py",args.source_root/"realstop/native_verifier.py",args.source_root/"realstop/evidence.py"]}, "timeout_s":30,"concurrency":2,"retries":0,"max_calls":args.max_calls,"entries":entries}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output/"run-manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise SystemExit("run manifest exists with different inputs")
    manifest_path.write_text(json.dumps(manifest,indent=2)+"\n")
    registered = args.output / "registered"
    for name, expected in manifest["source_files"].items():
        source = args.source_root / name
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit("source changed while freezing: " + name)
        target = registered / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (registered / "run-system-benchmark.py").write_bytes(Path(__file__).read_bytes())
    (registered / "cases.json").write_bytes(fixture.read_bytes())
    (registered / "freeze.json").write_bytes(freeze.read_bytes())
    if not args.execute:
        print("Frozen integration packets: "+str(len(entries)));return
    results_path = args.output/"predictions.jsonl"
    if results_path.exists():
        raise SystemExit("predictions already exist; refusing duplicate native calls")
    permitted = {e["case_id"] for e in entries if "packet" in e}
    permitted = set([e["case_id"] for e in entries if e["case_id"] in permitted][:args.max_calls])
    def run(entry):
        start = time.monotonic();captured=[]
        result={"case_id":entry["case_id"],"provider":entry["provider"],"packet_hash":entry.get("packet_hash"),"verifier_calls":0}
        if "parse_error" in entry:
            result.update(raw_verdict={"verdict":"INSUFFICIENT_EVIDENCE","reason":entry["parse_error"]},hook={"output":{},"status":"parser_error","verifier_called":False})
        elif entry["case_id"] not in permitted:
            result.update(raw_verdict={"verdict":"INSUFFICIENT_EVIDENCE","reason":"quota_cap_not_called"},hook={"output":{},"status":"quota_cap_not_called","verifier_called":False})
        else:
            def capture(packet, provider, model, timeout_s):
                verdict=verify_packet(packet,provider,model,timeout_s)
                captured.append(verdict)
                return verdict
            try:
                hook=evaluate_packet(entry["payload"],entry["packet"],provider=entry["provider"],mode="enforce",verifier=capture,timeout_s=30)
                result.update(raw_verdict=captured[0] if captured else {"verdict":"INSUFFICIENT_EVIDENCE","reason":hook["status"]},hook=hook,verifier_calls=len(captured))
            except Exception as exc:
                result.update(raw_verdict={"verdict":"INSUFFICIENT_EVIDENCE","reason":type(exc).__name__+":"+str(exc)},hook={"output":{},"status":"runner_exception","verifier_called":bool(captured)},verifier_calls=len(captured))
        result["latency_ms"]=(time.monotonic()-start)*1000
        return result
    results=[]
    with results_path.open("x") as stream, ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run,e) for e in entries]):
            result=future.result();results.append(result)
            stream.write(json.dumps(result,sort_keys=True)+"\n");stream.flush()
            print(json.dumps(result,sort_keys=True),flush=True)
    by={c["id"]:c for c in cases}
    report={}
    for arm in ("native", "nativehook"):
        semantic_groups = {json.dumps(by[r['case_id']]['packet'],sort_keys=True) for r in results}
        g={"n":len(results),"tp":0,"tn":0,"fp":0,"fn":0,"abstentions":0,"live_disposition_correct":0,"verifier_calls":sum(r["verifier_calls"] for r in results),"unique_authored_packet_groups":len(semantic_groups),"unique_parsed_packet_groups":len({r["packet_hash"] for r in results if r["packet_hash"]})}
        for r in results:
            c=by[r["case_id"]];v=r["raw_verdict"].get("verdict")
            pred={"CONFIRM_QUIT":"QUIT","LEGITIMATE_STOP":"STOP"}.get(v,"ABSTAIN") if arm=="native" else ("QUIT" if r["hook"].get("output",{}).get("decision")=="block" else "STOP")
            if pred=="ABSTAIN":g["abstentions"]+=1
            else:g[("tp" if c["gold"]=="QUIT" else "fp") if pred=="QUIT" else ("fn" if c["gold"]=="QUIT" else "tn")]+=1
            g["live_disposition_correct"]+=("block" if pred=="QUIT" else "allow")==c["expected_live_behavior"]
        times=sorted(r["latency_ms"] for r in results);g["latency_mean_ms"]=sum(times)/len(times);g["latency_p95_ms"]=times[min(len(times)-1,int(len(times)*.95))]
        g["accuracy_all"]=(g["tp"]+g["tn"])/g["n"];report[arm]=g
    (args.output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__=="__main__":main()

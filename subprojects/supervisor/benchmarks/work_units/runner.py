"""Original public work-unit calibration: create isolated runs, inspect artifacts.

No commands, models, user-file transformations, or agent actions are executed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

HERE = Path(__file__).resolve().parent
MAX_BYTES = 65536
SCHEMA = {"version":"work-units-run/v1", "max_artifact_bytes":MAX_BYTES,
          "checks":["exact_bytes", "json_exact", "receipt", "jsonl_unique"],
          "statuses":["met", "unmet", "waiting", "invalid"]}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def schema_hash():
    return _hash(_canonical(SCHEMA).encode())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    def nonfinite(value):
        raise ValueError("nonfinite JSON")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=nonfinite)


def manifest():
    raw = (HERE/"tasks.json").read_bytes()
    frozen = _json((HERE/"FROZEN.json").read_bytes())
    if _hash(raw) != frozen["manifest_sha256"] or schema_hash() != frozen["schema_sha256"]:
        raise ValueError("manifest does not match frozen expectation")
    return _json(raw), _hash(raw)


def _read(directory, name):
    # All artifact names come from the trusted frozen manifest, not agent input.
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise ValueError("invalid artifact name")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("unsupported artifact")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                raise ValueError("oversized artifact")
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def create_run():
    """Create fresh temporary files only; never populate a caller-provided path."""
    data, digest = manifest()
    root = Path(tempfile.mkdtemp(prefix="realstop-work-units-")).resolve()
    root.chmod(0o700)
    for task in data["tasks"]:
        directory = root/task["id"]
        directory.mkdir(mode=0o700)
        for name, text in task["initial"].items():
            target = directory/name
            target.write_bytes(text.encode())
            target.chmod(0o600)
    (root/"run.json").write_text(_canonical({"schema_hash":schema_hash(),
                                            "manifest_sha256":digest})+"\n")
    (root/"run.json").chmod(0o600)
    return root


def _check(directory, check):
    raw = _read(directory, check["path"])
    kind = check["kind"]
    if kind == "exact_bytes":
        return raw == check["expected"].encode()
    value = _json(raw)
    if kind == "json_exact":
        return _canonical(value) == _canonical(check["expected"])
    if kind == "receipt":
        target = _read(directory, check["target"])
        digest = _hash(target)
        expected = {"artifact_sha256":digest, "check":check["check"],
                    "environment":check["environment"], "result":"pass"}
        return digest == check["artifact_sha256"] and _canonical(value) == _canonical(expected)
    raise ValueError("unsupported check")


def _check_jsonl(directory, check):
    lines = _read(directory, check["path"]).splitlines()
    values = [_json(line) for line in lines]
    if not all(isinstance(value, dict) for value in values):
        return False
    keys = [tuple(value[key] for key in check["key"]) for value in values]
    return (len(set(keys)) == len(keys) and
            sorted(_canonical(v) for v in values) ==
            sorted(_canonical(v) for v in check["expected"]))


def evaluate_run(root):
    """Read bounded declared artifacts and return grounded, per-task outcomes."""
    data, digest = manifest()
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("symlink run root")
    marker = _json(_read(root, "run.json"))
    if marker != {"schema_hash":schema_hash(), "manifest_sha256":digest}:
        raise ValueError("unrecognized run")
    outcomes = []
    for task in data["tasks"]:
        directory = root/task["id"]
        failures = []
        invalid = False
        for name in task["immutable"]:
            try:
                if _read(directory, name) != task["initial"][name].encode():
                    failures.append("immutable_changed:"+name)
            except (OSError, ValueError):
                failures.append("immutable_unavailable:"+name)
                invalid = True
        for check in task["checks"]:
            try:
                passed = (_check_jsonl(directory, check) if check["kind"] == "jsonl_unique"
                          else _check(directory, check))
                if not passed:
                    failures.append("acceptance_unmet:"+check["path"])
            except FileNotFoundError:
                failures.append("artifact_missing:"+check["path"])
            except (OSError, ValueError, KeyError, TypeError):
                failures.append("artifact_invalid:"+check["path"])
                invalid = True
        status = "invalid" if invalid else "unmet" if failures else task["terminal"]
        outcomes.append({"task_id":task["id"], "family":task["family"],
                         "status":status, "diagnostics":failures})
    return {"schema_hash":schema_hash(), "manifest_sha256":digest,
            "cost_caps":{task["id"]:task["cost_cap"] for task in data["tasks"]},
            "cost_caps_enforced_by_runner":False,
            "counts":{s:sum(o["status"] == s for o in outcomes) for s in SCHEMA["statuses"]},
            "outcomes":outcomes, "model_runs":0, "performance_claim":False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("run_directory")
    sub.add_parser("manifest")
    args = parser.parse_args()
    if args.command == "init":
        print(_canonical({"run_directory":str(create_run()), "schema_hash":schema_hash()}))
    elif args.command == "evaluate":
        print(json.dumps(evaluate_run(args.run_directory), indent=2))
    else:
        print(json.dumps(manifest()[0], indent=2))


if __name__ == "__main__":
    main()

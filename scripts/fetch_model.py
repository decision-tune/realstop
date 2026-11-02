#!/usr/bin/env python3
"""Fetch the RealStop local judge (a DecisionTune pack) and verify it. Python 3.9+, standard library only.

    python3 scripts/fetch_model.py --dest ~/.claude/realstop/model/realstop-quit
    options: --base-url URL   fetch from here instead of the Hugging Face repo (a file:// URL works; used by the tests)
             --files PATH     the pinned file list (default: scripts/model-files.txt next to this script)

Every file is downloaded into a temporary folder, checked against the sha256 and byte size pinned in the file list,
and only then moved into place, so a half-finished or tampered download never becomes the model. Any failure leaves
the destination untouched and exits non-zero; realstop setup then says the phrase rules are active. Nothing but the model
files is requested, and no credentials or personal data are sent.

Exit codes: 0 model present and verified, 1 download or verification failed, 2 bad arguments, 3 hashes or MODEL_REVISION not pinned yet.
"""
import argparse, hashlib, os, re, shutil, sys, tempfile, urllib.request

# The ONE place the model's home is named. MODEL_REVISION is pinned to a commit hash so the bytes cannot change
# under the pinned sha256 values (fetch_model refuses to download from anything but a 40-hex commit hash).
MODEL_REPO = "decision-tune/realstop"
MODEL_REVISION = "6d40b7840b24671e456c822c5268e6137ed70c42"  # the Hugging Face commit that holds the files in model-files.txt

HERE = os.path.dirname(os.path.abspath(__file__))


def read_list(path):
    """Lines: '<sha256> <bytes> <relative path>'. Returns [(sha, size, rel)]; raises ValueError when not pinned."""
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            raise ValueError("bad line in file list: %r" % line[:60])
        sha, size, rel = parts
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha) or not size.isdigit():
            raise ValueError("not pinned yet (TBD hash or size) for %s" % rel)
        if os.path.isabs(rel) or ".." in rel.split("/") or rel.startswith("~"):
            raise ValueError("unsafe path in file list: %s" % rel)
        rows.append((sha, int(size), rel))
    if not rows:
        raise ValueError("the file list pins no files yet (TBD)")
    return rows


def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verified(dest, rows):
    for sha, size, rel in rows:
        p = os.path.join(dest, rel)
        if not os.path.isfile(p) or os.path.getsize(p) != size or sha_of(p) != sha:
            return False
    return True


def download(url, out, sha, size):
    h = hashlib.sha256()
    n = 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as r, open(out, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > size:
                raise ValueError("more bytes than pinned")
            h.update(chunk)
            f.write(chunk)
    if n != size or h.hexdigest() != sha:
        raise ValueError("size or sha256 mismatch")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--base-url", default=os.environ.get("REALSTOP_MODEL_BASE_URL") or os.environ.get("RESILIENT_MODEL_BASE_URL", ""))  # env: tests only
    ap.add_argument("--files", default=os.environ.get("REALSTOP_MODEL_FILES") or os.environ.get("RESILIENT_MODEL_FILES") or os.path.join(HERE, "model-files.txt"))  # env: tests only
    a = ap.parse_args(argv)
    dest = os.path.abspath(os.path.expanduser(a.dest))
    try:
        rows = read_list(a.files)
    except (OSError, ValueError) as e:
        print("fetch_model: no verified model to fetch: %s" % e, file=sys.stderr)
        return 3
    if not a.base_url and not re.fullmatch(r"[0-9a-f]{40}", MODEL_REVISION):  # a branch name can move under the pinned hashes
        print("fetch_model: no verified model to fetch: MODEL_REVISION is not a pinned commit hash yet (TBD)", file=sys.stderr)
        return 3
    if verified(dest, rows):
        print("fetch_model: model already present and verified at %s" % dest)
        return 0
    if os.path.exists(dest) and os.listdir(dest) and not os.path.isfile(os.path.join(dest, "pack.json")):
        print("fetch_model: %s exists, is not empty and holds no pack.json; not touching it" % dest, file=sys.stderr)
        return 1
    base = a.base_url or "https://huggingface.co/%s/resolve/%s" % (MODEL_REPO, MODEL_REVISION)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".model-dl-", dir=os.path.dirname(dest))
    try:
        for sha, size, rel in rows:
            print("fetch_model: %s (%d bytes)" % (rel, size))
            download(base.rstrip("/") + "/" + rel, os.path.join(tmp, rel), sha, size)
        if os.path.exists(dest):
            shutil.rmtree(dest)  # only ever an earlier incomplete or outdated copy of this model (checked above)
        os.replace(tmp, dest)
        tmp = ""
    except Exception as e:  # network, disk, or verification: leave nothing half-installed
        print("fetch_model: failed (%s): %s" % (type(e).__name__, e), file=sys.stderr)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    print("fetch_model: model verified and installed at %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

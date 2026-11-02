#!/usr/bin/env python3
"""Fail when a file that ships holds text that must not ship.

    python3 scripts/leak_check.py [ROOT]

ROOT defaults to the repository this script lives in. The files checked are `git ls-files` when ROOT is a git
checkout, else every file under ROOT (never .git). Exit 0 when clean, 1 with one line per hit, 2 on a usage error.

Checked:
- internal notes tracked as files: *_VERIFY.md, and any .md or .txt file whose name holds "notes" or "handoff";
- the cloud provider's name in prose (see PROVIDER below for the identifiers that may stay);
- an internal review tool's name, "laptop", the owner's name and personal email, "/Users/", "decision-log",
  "scratchpad", "decision-battery".
"""
import base64, os, re, subprocess, sys

# This file and its test hold the patterns themselves, so they are not scanned.
SELF = {"scripts/leak_check.py", "tests/leak.sh"}

# The owner's name and email are stored encoded so that this public file does not itself carry them.
_NAME, _SURNAME, _EMAIL = (base64.b64decode(x).decode() for x in ("QWJl", "RGlheg==", "YWJlMjM4QGdtYWlsLmNvbQ=="))

TERMS = [
    ("review tool name", re.compile(r"\bastra", re.I)),
    ("laptop", re.compile(r"laptop", re.I)),
    ("owner name", re.compile(r"\b%s\b" % _NAME, re.I)),
    ("owner surname", re.compile(_SURNAME, re.I)),  # also catches the user name built on it
    ("owner email", re.compile(re.escape(_EMAIL.split("@")[0]), re.I)),
    ("home path", re.compile(r"/Users/")),
    ("decision-log", re.compile(r"decision-log", re.I)),
    ("scratchpad", re.compile(r"scratchpad", re.I)),
    ("decision-battery", re.compile(r"decision-battery", re.I)),
]

# Per-path exceptions, each with its reason. Nothing else is excused.
ALLOW = {
    # Synthetic home paths that the redaction tests feed to the log masker; the test needs a real-looking home path.
    "home path": [(re.compile(r"^tests/"), re.compile(r"/Users/(someone|me)/"))],
    # A verbatim copy of the public RealStopBench synthetic set (invented text); editing it would make it differ from
    # the released benchmark that the learn gate is defined on.
    "laptop": [(re.compile(r"^data/regression_pairs\.jsonl$"), re.compile(r"laptop", re.I))],
}

# The provider. Prose says "cloud decision model". What may stay is the code spelling of the judge and its plumbing:
# the lowercase judge value `jev` (CLI `realstop judge jev`, `--judge jev`, config "jev", function and file names),
# identifiers built on JEV with an underscore (JEV_TIMEOUT_S, NO_QUIT_JEV, KEEP_JEV), and three fixed names the code
# must use: the key file typesafe.key, the env var TYPESAFE_API_KEY and the API host api.typesafe.ai.
# Capitalized "Jev", a standalone "JEV" or any other spelling of the provider name is prose and fails.
PROVIDER_IDS = re.compile(r"typesafe\.key|TYPESAFE_API_KEY|api\.typesafe\.ai|\w*_JEV\w*|JEV_\w*")
PROVIDER_PROSE = re.compile(r"(?i:typesafe)|\bJev\b|\bJEV\b")  # lowercase jev is the identifier spelling
PROVIDER_ANY = re.compile(r"typesafe|jev", re.I)
CODE_SPAN = re.compile(r"`[^`\n]+`")


def files(root):
    if os.path.isdir(os.path.join(root, ".git")):
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)
    found = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        found += [os.path.relpath(os.path.join(d, n), root) for n in names]
    return sorted(found)


def internal_file(path):
    base = os.path.basename(path)
    if base.endswith("_VERIFY.md"):
        return True
    # Only prose files count: tests/regressions/handoff/*.json are fixtures for the hand-off rule, not notes.
    return base.lower().endswith((".md", ".txt")) and re.search(r"notes|handoff", base, re.I) is not None


def provider_hits(path, line, in_fence):
    """Provider mentions in one line that are prose, not code."""
    if path.endswith(".md"):
        if in_fence:
            return PROVIDER_PROSE.findall(PROVIDER_IDS.sub("", line))
        spans = CODE_SPAN.findall(line)
        outside = CODE_SPAN.sub("", line)
        return PROVIDER_ANY.findall(outside) + [h for s in spans for h in PROVIDER_PROSE.findall(PROVIDER_IDS.sub("", s))]
    return PROVIDER_PROSE.findall(PROVIDER_IDS.sub("", line))


def allowed(term, path, text):
    return any(p.search(path) and a.search(text) for p, a in ALLOW.get(term, ()))


def scan(root):
    hits = []
    paths = files(root)
    for p in paths:
        if internal_file(p):
            hits.append("%s: internal notes file is tracked" % p)
        if p in SELF:
            continue
        try:
            with open(os.path.join(root, p), encoding="utf-8") as f:
                lines = f.read().split("\n")
        except (UnicodeDecodeError, OSError):
            continue  # binary (the PDF report carries its author byline by design) or unreadable
        fence = False
        for n, line in enumerate(lines, 1):
            if p.endswith(".md") and line.lstrip().startswith("```"):
                fence = not fence
                continue
            for name, rx in TERMS:
                for m in rx.finditer(line):
                    rest = line[max(0, m.start() - 20):m.end() + 40]
                    if not allowed(name, p, rest):
                        hits.append("%s:%d: %s: %s" % (p, n, name, line.strip()[:120]))
                        break
            for h in provider_hits(p, line, fence):
                hits.append("%s:%d: provider name in prose (%s): %s" % (p, n, h, line.strip()[:120]))
    return hits, len(paths)


def main(argv):
    if len(argv) > 2:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    root = os.path.abspath(argv[1] if len(argv) == 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    hits, n = scan(root)
    for h in hits:
        print(h)
    if hits:
        print("leak_check: %d hit(s) in %d files" % (len(hits), n), file=sys.stderr)
        return 1
    print("leak_check: clean (%d files)" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

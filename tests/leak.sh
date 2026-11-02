#!/bin/bash
# scripts/leak_check.py: each planted leak fails, each allowed identifier passes, and this repo is clean.
# Run by tests/run.sh; also runnable alone: bash tests/leak.sh
set -uo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
py=/usr/bin/python3; [ -x "$py" ] || py="$(command -v python3)"
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: want $2, got $3"; fail=1; fi; }
# one <path> <content>: a fresh tree (not a git checkout) holding one file; prints leak_check's exit code
one() { d="$(mktemp -d "$t/tree.XXXXXX")"; mkdir -p "$d/$(dirname "$1")"; printf '%s\n' "$2" > "$d/$1"
        "$py" "$here/scripts/leak_check.py" "$d" > "$d.out" 2>&1; echo $?; }
# the owner's name and email, decoded the same way leak_check.py does, so this file does not carry them
NAME="$("$py" -c 'import base64; print(base64.b64decode("QWJl").decode())')"
MAIL="$("$py" -c 'import base64; print(base64.b64decode("YWJlMjM4QGdtYWlsLmNvbQ==").decode())')"
PROV="$("$py" -c 'print("Type" + "Safe")')"

check "leak: this repo is clean" 0 "$("$py" "$here/scripts/leak_check.py" "$here" > "$t/repo.out" 2>&1; echo $?)"
check "leak: the provider name in prose fails" 1 "$(one README.md "The judge runs on $PROV.")"
check "leak: the cloud model name in prose fails" 1 "$(one docs/a.md "Jev decides the stop.")"
check "leak: the judge value outside code in a doc fails" 1 "$(one docs/a.md "Pick jev for the cloud.")"
check "leak: the CLI command in backticks passes" 0 "$(one README.md 'Run `realstop judge jev` or `--judge jev`; `cloud_ok` and `--key-stdin` stay.')"
check "leak: the CLI command in a fenced block passes" 0 "$(one README.md "$(printf '```bash\nrealstop judge local|jev|rules\n```')")"
check "leak: code identifiers pass" 0 "$(one hooks/x.py 'JEV_TIMEOUT_S = 6; KEY = "typesafe.key"; URL = "https://api.typesafe.ai/v1"; judge = "jev"  # TYPESAFE_API_KEY')"
check "leak: the capitalized name in a code comment fails" 1 "$(one hooks/x.py '# Jev answers here')"
check "leak: the review tool name fails" 1 "$(one tests/x.sh '# found by Astra')"
check "leak: laptop fails" 1 "$(one docs/a.md 'Tested on my laptop.')"
check "leak: laptop in the released synthetic pairs passes" 0 "$(one data/regression_pairs.jsonl '{"request": "on my laptop"}')"
check "leak: the owner's first name fails" 1 "$(one docs/a.md "Ask $NAME first.")"
check "leak: the owner's email fails" 1 "$(one docs/a.md "Mail $MAIL.")"
check "leak: a home path fails" 1 "$(one docs/a.md 'See /Users/pat/proj.')"
check "leak: a synthetic home path in a test passes" 0 "$(one tests/x.sh 'mask /Users/someone/proj')"
check "leak: decision-log, scratchpad, decision-battery fail" "1 1 1" "$(one a.md 'see decision-log.md') $(one a.md 'a scratchpad file') $(one a.md 'decision-battery-x')"
check "leak: an internal notes file fails" "1 1 1" "$(one RELEASE_VERIFY.md 'ok') $(one docs/notes.md 'ok') $(one HANDOFF.txt 'ok')"
check "leak: a handoff fixture is not a notes file" 0 "$(one tests/regressions/handoff/1_x.json '{}')"
d="$t/named"; mkdir -p "$d/docs"; echo 'Tested on my laptop.' > "$d/docs/a.md"
o="$("$py" "$here/scripts/leak_check.py" "$d" 2>/dev/null)"
check "leak: a hit names the file, line and term" yes "$(case "$o" in ("docs/a.md:1: laptop: "*) echo yes ;; (*) echo no ;; esac)"
exit $fail

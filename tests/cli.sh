#!/bin/bash
# `realstop` command-line tests: temp HOME, fake launchctl, no network. Run by tests/run.sh; also runnable alone:
#   bash tests/cli.sh
# The command runs from this clone (python3 -m realstop) on the oldest Python the package supports on this machine.
set -uo pipefail
export REALSTOP_MODEL_FILES=/nonexistent  # setup never downloads in tests
here="$(cd "$(dirname "$0")/.." && pwd)"
py=/usr/bin/python3; [ -x "$py" ] || py="$(command -v python3)"
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: want $2, got $3"; fail=1; fi; }
has() { case "$1" in (*"$2"*) echo yes ;; (*) echo no ;; esac; }
mkdir -p "$t/fakebin"; printf '#!/bin/sh\necho "$@" >> "%s/launchctl.log"\n' "$t" > "$t/fakebin/launchctl"; chmod +x "$t/fakebin/launchctl"
printf '#!/bin/sh\nexit 1\n' > "$t/fakebin/secrets"; chmod +x "$t/fakebin/secrets"  # a secrets store with no key
newhome() { h="$t/$1"; rm -rf "$h"; mkdir -p "$h/.claude"; echo "$h"; }
rs() { HOME="$h" PATH="$t/fakebin:$PATH" PYTHONPATH="$here" "$py" -m realstop "$@" </dev/null 2>&1; }
cfg() { jq -r "$2" "$1/.claude/realstop/config.json"; }
SEED='{"model":"opus","hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo other-stop"}]}]}}'
cnt() { jq -r '[.hooks[]?[]?.hooks[]? | select(.command | test("/realstop/(no-quit|correction|learn)\\.py"))] | length' "$1"; }

# ---- usage ----
h="$(newhome u)"
rs bogus >/dev/null; check "an unknown subcommand exits 2" 2 "$?"
rs >/dev/null; check "no subcommand exits 2" 2 "$?"
rs setup --judge nope >/dev/null; check "setup rejects an unknown judge (exit 2)" 2 "$?"
rs setup --strength max >/dev/null; check "setup rejects an unknown strength (exit 2)" 2 "$?"
rs setup --port 1.2.3.4 >/dev/null; check "setup rejects a non-numeric port (exit 2)" 2 "$?"
rs judge >/dev/null; check "judge with no value exits 2" 2 "$?"
check "--version prints the package version" "realstop $(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/realstop/__init__.py")" "$(rs --version)"
o="$(rs --help)"; check "--help lists every command" "yes yes yes yes yes yes yes yes yes yes" "$(for c in setup judge strength learn pause resume status diagnose serve uninstall; do has "$o" "$c"; done | tr '\n' ' ' | sed 's/ $//')"
check "status before install says so" "yes" "$(has "$(rs status)" "not installed")"

# ---- setup, non-interactive, each judge ----
h="$(newhome s1)"; echo "$SEED" > "$h/.claude/settings.json"
o="$(rs setup --judge rules)"; rc=$?
check "setup --judge rules: exit 0, judge phrases, no cloud" "0 phrases false" "$rc $(cfg "$h" '.judge') $(cfg "$h" '.cloud_ok')"
check "setup registers four entries and keeps the other hook" "4 yes" "$(cnt "$h/.claude/settings.json") $(jq -e '.hooks.Stop[] | select(.hooks[0].command == "echo other-stop")' "$h/.claude/settings.json" >/dev/null && echo yes || echo no)"
check "setup installs hooks, skill, launcher, manifest (0444), standing.txt" "yes yes yes 444 yes" "$(test -f "$h/.claude/realstop/no-quit.py" && echo yes || echo no) $(test -f "$h/.claude/skills/realstop/SKILL.md" && echo yes || echo no) $(test -x "$h/.claude/realstop/realstop" && echo yes || echo no) $(stat -f %Lp "$h/.claude/realstop/manifest.json" 2>/dev/null || stat -c %a "$h/.claude/realstop/manifest.json") $(test -f "$h/.claude/realstop/standing.txt" && echo yes || echo no)"
check "setup writes install.json and the settings backup" "yes 1" "$(jq -e .installed_at "$h/.claude/realstop/install.json" >/dev/null && echo yes || echo no) $(ls "$h/.claude" | grep -c 'bak-realstop')"
sum1="$(shasum "$h/.claude/settings.json")"; rs setup >/dev/null
check "a re-run is idempotent and keeps the stored judge" "$sum1 phrases" "$(shasum "$h/.claude/settings.json") $(cfg "$h" '.judge')"
h="$(newhome s2)"; o="$(rs setup --judge local --port 8791 --model "$t/nomodel")"
check "setup --judge local: judge, port, model, no cloud" "local http://127.0.0.1:8791 $t/nomodel false" "$(cfg "$h" '.judge') $(cfg "$h" '.local_url') $(cfg "$h" '.model_dir') $(cfg "$h" '.cloud_ok')"
check "setup without a model says the rules are active" yes "$(has "$o" "phrase rules are active")"
h="$(newhome s3)"; printf 'sk-cli-test\n' | HOME="$h" PATH="$t/fakebin:$PATH" PYTHONPATH="$here" "$py" -m realstop setup --judge jev --key-stdin > "$t/jev.txt" 2>&1
check "setup --judge jev --key-stdin: cloud_ok true, key 0600, key never printed" "jev true 600 no" "$(cfg "$h" '.judge') $(cfg "$h" '.cloud_ok') $(stat -f %Lp "$h/.claude/realstop/typesafe.key" 2>/dev/null || stat -c %a "$h/.claude/realstop/typesafe.key") $(has "$(cat "$t/jev.txt")" sk-cli-test)"
h="$(newhome s4)"; o="$(rs setup)"
check "setup with no flags and no terminal: local at moderate, one line on how to change it" "local moderate false yes" "$(cfg "$h" '.judge') $(cfg "$h" '.strength') $(cfg "$h" '.cloud_ok') $(has "$o" "realstop judge local|jev|rules")"
h="$(newhome s5)"; o="$(printf '3\n' | REALSTOP_ASK=1 HOME="$h" PATH="$t/fakebin:$PATH" PYTHONPATH="$here" "$py" -m realstop setup 2>&1)"
check "setup asks on a first run (3 picks rules) and never again" "phrases yes no" "$(cfg "$h" '.judge') $(has "$o" "Which judge") $(has "$(printf '1\n' | REALSTOP_ASK=1 HOME="$h" PATH="$t/fakebin:$PATH" PYTHONPATH="$here" "$py" -m realstop setup 2>&1)" "Which judge")"
h="$(newhome s6)"; o="$(rs setup -y --strength hard)"
check "setup --strength sets it and ends gentle mode" "hard true" "$(cfg "$h" '.strength') $(jq -r .strength_set "$h/.claude/realstop/install.json")"

# ---- judge / strength switches (the manifest is regenerated each time) ----
h="$(newhome j)"; rs setup --judge rules >/dev/null
rs judge local >/dev/null; check "judge local" "local false" "$(cfg "$h" '.judge') $(cfg "$h" '.cloud_ok')"
rs judge jev >/dev/null; check "judge jev opts in to the cloud" "jev true" "$(cfg "$h" '.judge') $(cfg "$h" '.cloud_ok')"
rs judge rules >/dev/null; check "judge rules stores phrases and drops the opt-in" "phrases false" "$(cfg "$h" '.judge') $(cfg "$h" '.cloud_ok')"
rs judge phrases >/dev/null; check "judge phrases (the stored name) is still accepted" "phrases" "$(cfg "$h" '.judge')"
rs strength light >/dev/null; check "strength light" "light" "$(cfg "$h" '.strength')"
rs strength hard >/dev/null; check "strength hard" "hard" "$(cfg "$h" '.strength')"
check "the manifest matches config.json after a switch" "$(shasum -a 256 < "$h/.claude/realstop/config.json" | cut -d' ' -f1)" "$(jq -r '.files["config.json"]' "$h/.claude/realstop/manifest.json")"
rs strength max >/dev/null; check "strength max exits 2 and changes nothing" "2 hard" "$? $(cfg "$h" '.strength')"

# ---- pause / resume / status ----
h="$(newhome p)"; rs setup --judge rules >/dev/null
o="$(rs pause)"; check "pause writes control.json and says how to resume" "true yes" "$(jq -r .paused "$h/.claude/realstop/control.json") $(has "$o" "realstop resume")"
o="$(rs status)"; check "status shows judge, strength, paused, server, corrections, versions" "yes yes yes yes yes yes yes" "$(has "$o" "judge:        rules") $(has "$o" "strength:") $(has "$o" "paused:       YES") $(has "$o" "server:") $(has "$o" "corrections:  0") $(has "$o" "versions:") $(has "$o" "hooks up to date")"
rs resume >/dev/null; check "resume" "false" "$(jq -r .paused "$h/.claude/realstop/control.json")"
check "status after resume" "yes" "$(has "$(rs status)" "paused:       no")"
printf '%s\n' '{"at":100,"session":"s2","hits":[],"judge":"jev","block":false,"tail":"x"}' > "$h/.claude/realstop/stops.jsonl"
printf '%s\n' '{"at":200,"session":"s2","kind":"missed_quit","prompt":"no"}' > "$h/.claude/realstop/corrections.jsonl"
check "status counts corrections waiting for review" "yes" "$(has "$(rs status)" "corrections:  1 waiting")"
echo "# edited" >> "$h/.claude/realstop/no-quit.py"; check "status flags hooks that differ from the package" yes "$(has "$(rs status)" "differs from this package")"
h="$(newhome p2)"; rs setup --judge local --model "$t/nomodel" >/dev/null
check "status with a missing local model says so" yes "$(has "$(rs status)" "no local model at $t/nomodel")"

# ---- diagnose, learn ----
o="$(rs diagnose)"; check "diagnose prints the redacted summary" "yes yes" "$(has "$o" "RealStop diagnostics") $(has "$o" "judge=local")"
check "diagnose leaks no home path" no "$(has "$o" "$h")"
h="$(newhome dm)"; mkdir -p "$t/dmmodel"; echo '{}' > "$t/dmmodel/pack.json"; echo '{"x":1}' > "$t/dmmodel/thresholds.json"; rs setup --judge local --model "$t/dmmodel" >/dev/null
check "diagnose: a model folder with thresholds.json still matches the manifest" yes "$(has "$(rs diagnose)" "matches the installed hooks and config")"
o="$(rs learn --status)"; check "learn --status runs the learn script" "yes" "$(has "$o" "pending corrections:")"
rs learn --bogus >/dev/null; check "learn with an unknown flag exits 2" 2 "$?"

# ---- serve --keep (macOS LaunchAgent, fake launchctl) ----
if [ "$(uname -s)" = Darwin ]; then
  h="$(newhome k)"; rs setup --judge rules >/dev/null; rs serve --keep >/dev/null; rc=$?
  check "serve --keep without a local model exits 1 and writes no agent" "1 no" "$rc $(test -e "$h/Library/LaunchAgents/com.realstop.model-server.plist" && echo yes || echo no)"
  mkdir -p "$t/okmodel"; echo '{}' > "$t/okmodel/pack.json"; rs judge local --model "$t/okmodel" --port 8792 >/dev/null; rs serve --keep >/dev/null; rc=$?
  check "serve --keep installs the LaunchAgent and bootstraps it" "0 com.realstop.model-server 1" "$rc $(plutil -convert json -o - "$h/Library/LaunchAgents/com.realstop.model-server.plist" | jq -r .Label) $(grep -c bootstrap "$t/launchctl.log")"
  rs serve --unkeep >/dev/null; check "serve --unkeep removes it" no "$(test -e "$h/Library/LaunchAgents/com.realstop.model-server.plist" && echo yes || echo no)"
fi

# ---- uninstall ----
h="$(newhome x)"; echo "$SEED" > "$h/.claude/settings.json"; mkdir -p "$h/.claude/hooks"; echo canary > "$h/.claude/hooks/other.sh"
rs setup --judge rules >/dev/null; echo mine >> "$h/.claude/realstop/standing.txt"
o="$(rs uninstall)"; rc=$?
check "uninstall: exit 0, settings.json back to the original, no RealStop entries" "0 $(echo "$SEED" | jq -S .) 0" "$rc $(jq -S . "$h/.claude/settings.json") $(cnt "$h/.claude/settings.json")"
check "uninstall: files and skill gone, standing.txt and other tools' files kept" "no no yes canary" "$(test -e "$h/.claude/realstop/no-quit.py" && echo yes || echo no) $(test -e "$h/.claude/skills/realstop/SKILL.md" && echo yes || echo no) $(test -e "$h/.claude/realstop/standing.txt" && echo yes || echo no) $(cat "$h/.claude/hooks/other.sh")"
check "uninstall: a second run is a clean no-op" 0 "$(rs uninstall >/dev/null; echo $?)"
check "uninstall names how to remove the command" yes "$(has "$o" "uv tool uninstall realstop")"

# ---- packaging ----
check "the console script points at cli:main" 'realstop = "realstop.cli:main"' "$(grep '^realstop = ' "$here/pyproject.toml")"
missing=""; for f in $(sed -n '/force-include\]/,/^$/s/^"\([^"]*\)" = .*/\1/p' "$here/pyproject.toml"); do [ -e "$here/$f" ] || missing="$missing $f"; done
check "every file the wheel bundles exists in the clone" "" "$missing"
check "the bundled layout is the one paths.res_root() expects" "no-quit.py correction.py learn.py" "$(sed -n 's#^"hooks/\([a-z.-]*\)" = "realstop/_res/hooks/.*#\1#p' "$here/pyproject.toml" | tr '\n' ' ' | sed 's/ $//')"
check "the CLI imports only the standard library" "" "$(grep -hE '^(import|from) ' "$here"/realstop/*.py | grep -vE '^from (\.|__future__)|^import (argparse|getpass|hashlib|json|os|platform|re|shutil|subprocess|sys|time|urllib\.error|urllib\.request|plistlib)$|^from (pathlib|typing|importlib\.metadata) import' )"
exit $fail

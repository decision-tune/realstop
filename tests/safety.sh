#!/bin/bash
# Safety tests: the owner's instant bypass, new-install gentleness, safe settings.json edits, uninstall, diagnostics and
# the loopback-only server. Offline, temp HOME, fake launchctl. Run by tests/run.sh; also runnable alone:
#   bash tests/safety.sh            (HOOKS_DIR=dir tests the hooks in dir, as tests/mutants.sh does)
set -uo pipefail
export REALSTOP_FROM_SOURCE=1 REALSTOP_MODEL_FILES=/nonexistent  # install.sh runs the command from this clone; installs never download
here="$(cd "$(dirname "$0")/.." && pwd)"
hooks="${HOOKS_DIR:-$here/hooks}"
py=/usr/bin/python3; [ -x "$py" ] || py="$(command -v python3)"
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: want $2, got $3"; fail=1
  # under tests/mutants.sh the run is over once the mutant's named test has failed
  if [ -n "${MUTANT_TEST:-}" ]; then case "$1" in ("$MUTANT_TEST"*) exit 1 ;; esac; fi; fi; }
has() { case "$1" in (*"$2"*) echo yes ;; (*) echo no ;; esac; }
mkdir -p "$t/fakebin"; printf '#!/bin/sh\nexit 0\n' > "$t/fakebin/launchctl"; chmod +x "$t/fakebin/launchctl"
newhome() { h="$t/$1"; rm -rf "$h"; mkdir -p "$h/.claude/projects/p"; echo "$h"; }
inst() { h="$1"; shift; HOME="$h" PATH="$t/fakebin:$PATH" bash "$here/install.sh" "$@" </dev/null 2>&1; }
mk() { f="$1"; u="$2"; shift 2; jq -cn --arg u "$u" '{type:"user",message:{content:$u}}' > "$f"
  for n in "$@"; do jq -cn --arg n "$n" '{type:"assistant",message:{content:[{type:"tool_use",name:$n,input:{}}]}}' >> "$f"; done; }
OFFER="Done with the fix. Want me to push it?"
stopx() {  # stopx <home> <session> <active> <reply> [extra payload json]: the Stop hook, phrases judge -> block|pass
  rx="${5:-}"; [ -n "$rx" ] || rx='{}'
  o="$(jq -cn --arg s "$2" --arg p "$1/.claude/projects/p/tr.jsonl" --argjson a "$3" --arg m "$4" --argjson x "$rx" \
    '{session_id:$s,transcript_path:$p,stop_hook_active:$a,last_assistant_message:$m} + $x' | HOME="$1" NO_QUIT_JUDGE=phrases "$py" -I "$hooks/no-quit.py")"
  case "$o" in *'"block"'*) echo block ;; *) echo pass ;; esac; }
prompt() {  # prompt <home> <session> <text>: the UserPromptSubmit hook's stdout
  jq -cn --arg s "$2" --arg p "$3" '{session_id:$s,prompt:$p}' | HOME="$1" "$py" -I "$hooks/correction.py"; }

# ---- 1. instant bypass ----
h="$(newhome b)"; R="$h/.claude/realstop"; mkdir -p "$R"; mk "$h/.claude/projects/p/tr.jsonl" go
check "bypass: baseline offer blocks" block "$(stopx "$h" b1 false "$OFFER")"
out="$(prompt "$h" b1 "realstop off")"
check "bypass: 'realstop off' pauses and acknowledges" "true yes" "$(jq -r .paused "$R/control.json") $(has "$out" paused)"
check "bypass: 'realstop off' is not logged as a correction" no "$(test -s "$R/corrections.jsonl" && echo yes || echo no)"
check "bypass: paused, an offer passes" pass "$(stopx "$h" b1 false "$OFFER two")"
check "bypass: paused stop is logged as bypass" paused "$(tail -1 "$R/stops.jsonl" | jq -r .bypass)"
check "bypass: paused, other sessions pass too" pass "$(stopx "$h" b9 false "$OFFER three")"
prompt "$h" b1 "RealStop on." >/dev/null
check "bypass: 'RealStop on.' resumes (blocks again)" block "$(stopx "$h" b1 false "$OFFER four")"
prompt "$h" b1 "realstop off for a minute, thanks" >/dev/null
check "bypass: a sentence containing the words does not pause" "block" "$(stopx "$h" b1 false "$OFFER five")"
prompt "$h" b2 "let it stop" >/dev/null
check "bypass: 'let it stop' leaves other sessions alone" block "$(stopx "$h" b3 false "$OFFER six")"
sub="$(stopx "$h" b2 false "$OFFER seven" '{"hook_event_name":"SubagentStop","agent_type":"x","agent_id":"a1"}')"
check "bypass: a subagent stop passes under 'once' but does not consume it" "pass yes" "$sub $(jq -e '.once.b2' "$R/control.json" >/dev/null 2>&1 && echo yes || echo no)"
check "bypass: 'let it stop' lets that session's next stop through" pass "$(stopx "$h" b2 false "$OFFER eight")"
check "bypass: ...once only" block "$(stopx "$h" b2 false "$OFFER nine")"
echo "{\"once\":{\"b4\":$(( $(date +%s) - 7200 ))}}" > "$R/control.json"
check "bypass: an expired 'let it stop' is ignored" block "$(stopx "$h" b4 false "$OFFER ten")"
inst "$h" --pause >/dev/null
check "bypass: install.sh --pause pauses" pass "$(stopx "$h" b5 false "$OFFER eleven")"
inst "$h" --resume >/dev/null
check "bypass: install.sh --resume resumes" block "$(stopx "$h" b5 false "$OFFER twelve")"
check "bypass: control.json is not in the tamper manifest" no "$(jq -e '.files["control.json"]' "$R/manifest.json" >/dev/null 2>&1 && echo yes || echo no)"
echo '{broken' > "$R/control.json"
check "bypass: a corrupt control.json fails open to normal behavior" block "$(stopx "$h" b6 false "$OFFER thirteen")"
c1="$(prompt "$h" b7 "No, do it yourself now")"; c2="$(prompt "$h" b7 "No, do it yourself now")"
check "dedupe: a second registration of the correction hook handles the same prompt once" "1" "$(wc -l < "$R/corrections.jsonl" | tr -d ' ')"

# ---- 2. new-install gentleness ----
twoblocks() {  # twoblocks <home> <session>: first offer, then a second one after a tool call -> "block pass" when the cap is 1
  mk "$1/.claude/projects/p/tr.jsonl" go
  a="$(stopx "$1" "$2" false "$OFFER one")"
  mk "$1/.claude/projects/p/tr.jsonl" go Bash
  b="$(stopx "$1" "$2" true "Ran it. $OFFER again")"; echo "$a $b"; }
h="$(newhome g)"; inst "$h" --judge phrases >/dev/null
check "gentle: a fresh install writes install.json" yes "$(jq -e '.installed_at' "$h/.claude/realstop/install.json" >/dev/null 2>&1 && echo yes || echo no)"
check "gentle: first week, one nudge per turn" "block pass" "$(twoblocks "$h" g1)"
jq --argjson n "$(( $(date +%s) - 8 * 86400 ))" '.installed_at = $n' "$h/.claude/realstop/install.json" > "$h/ij" && mv "$h/ij" "$h/.claude/realstop/install.json"
check "gentle: after 7 days the configured cap applies" "block block" "$(twoblocks "$h" g2)"
h="$(newhome g2)"; inst "$h" --judge phrases >/dev/null; inst "$h" --judge phrases --strength moderate >/dev/null
check "gentle: --strength ends it" "block block" "$(twoblocks "$h" g3)"
check "gentle: a re-run does not restart the clock" "yes" "$(jq -e '.installed_at and .strength_set' "$h/.claude/realstop/install.json" >/dev/null 2>&1 && echo yes || echo no)"
h="$(newhome g3)"; mkdir -p "$h/.claude/realstop"; echo '{"judge":"phrases","cloud_ok":false}' > "$h/.claude/realstop/config.json"; inst "$h" >/dev/null
check "gentle: an install that already existed is not made gentle" "no block block" "$(test -e "$h/.claude/realstop/install.json" && echo yes || echo no) $(twoblocks "$h" g4)"

# ---- 3. settings.json safety ----
SEED='{"model":"opus","env":{"A":"1"},"permissions":{"allow":["Bash(ls)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo other-stop","timeout":3}]}],"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo pre"}]}],"SessionStart":[{"hooks":[{"type":"command","command":"/opt/x/learn.py"}]}]}}'
h="$(newhome s)"; echo "$SEED" > "$h/.claude/settings.json"; S="$h/.claude/settings.json"
inst "$h" --judge phrases >/dev/null
cnt() { jq -r '[.hooks[]?[]?.hooks[]? | select(.command | test("/realstop/(no-quit|correction|learn)\\.py"))] | length' "$1"; }
check "settings: four RealStop entries, unrelated keys and hooks kept" "4 opus 1 echo-pre yes" "$(cnt "$S") $(jq -r .model "$S") $(jq -r .env.A "$S") $(jq -r '.hooks.PreToolUse[0].hooks[0].command' "$S" | tr ' ' '-') $(jq -e '.hooks.Stop[] | select(.hooks[0].command == "echo other-stop")' "$S" >/dev/null && echo yes || echo no)"
check "settings: someone else's learn.py in another folder is not touched" yes "$(jq -e '.hooks.SessionStart[] | select(.hooks[0].command == "/opt/x/learn.py")' "$S" >/dev/null && echo yes || echo no)"
b1="$(ls "$h/.claude" | grep -c 'bak-realstop')"; sum1="$(shasum "$S")"
inst "$h" --judge phrases >/dev/null
check "settings: re-run is idempotent (same bytes, no new backup)" "$sum1 $b1" "$(shasum "$S") $(ls "$h/.claude" | grep -c 'bak-realstop')"
check "settings: the backup is the original" "$(echo "$SEED" | jq -S .)" "$(jq -S . "$h/.claude/$(ls "$h/.claude" | grep 'bak-realstop' | head -1)")"
# both install orders with a RealStop plugin
h="$(newhome so1)"; echo '{"enabledPlugins":{"realstop@market":true}}' > "$h/.claude/settings.json"; o="$(inst "$h" --judge phrases)"
check "settings: plugin first, then install.sh adds no second set of hooks" "0 yes" "$(cnt "$h/.claude/settings.json") $(has "$o" "no double registration")"
h="$(newhome so2)"; inst "$h" --judge phrases >/dev/null; jq '.enabledPlugins = {"realstop@market": true}' "$h/.claude/settings.json" > "$h/x" && mv "$h/x" "$h/.claude/settings.json"; inst "$h" --judge phrases >/dev/null
check "settings: install.sh first, then plugin, then install.sh again leaves one set" 0 "$(cnt "$h/.claude/settings.json")"
# plugin packaging: manifest, marketplace, hooks wiring; the plugin's own hook commands run, and with install.sh's copy also
# registered (either order) a stop is judged once. The plugin id the real CLI writes is realstop@decision-tune.
pj="$here/.claude-plugin/plugin.json"; mj="$here/.claude-plugin/marketplace.json"; hj="$here/hooks/hooks.json"
check "plugin: manifest and marketplace name realstop in decision-tune, skills dir has the skill" "realstop decision-tune realstop yes" "$(jq -r .name "$pj") $(jq -r .name "$mj") $(jq -r '.plugins[0].name' "$mj") $(test -f "$here/skills/realstop/SKILL.md" && echo yes)"
check "plugin: hooks.json wires the four events to the plugin root" "SessionStart Stop SubagentStop UserPromptSubmit" "$(jq -r '.hooks | keys | join(" ")' "$hj")"
check "plugin: all four hooks.json commands point at a hook that exists" "4" "$(jq -r '.hooks[][].hooks[].command' "$hj" | sed -n 's/.*CLAUDE_PLUGIN_ROOT}\/\(hooks\/[a-z-]*\.py\).*/\1/p' | while read -r f; do test -f "$here/$f" && echo ok; done | wc -l | tr -d ' ')"
check "plugin: install.sh's enabledPlugins guard matches the plugin id the CLI writes" "0 yes" "$(h="$(newhome pid)"; echo '{"enabledPlugins":{"realstop@decision-tune":true}}' > "$h/.claude/settings.json"; o="$(inst "$h" --judge phrases)"; echo "$(cnt "$h/.claude/settings.json") $(has "$o" "no double registration")")"
if command -v claude >/dev/null 2>&1; then
  check "plugin: claude plugin validate --strict passes" 0 "$(cd "$here" && claude plugin validate --strict . >/dev/null 2>&1; echo $?)"
else echo "SKIP plugin: claude CLI not installed, validate not run"; fi
h="$(newhome pd)"; mk "$h/.claude/projects/p/tr.jsonl" go; inst "$h" --judge phrases >/dev/null
pstop() {  # pstop <home> <session>: the Stop hook as the plugin runs it (hooks.json command through sh, plugin root set)
  cmd="$(jq -r '.hooks.Stop[0].hooks[0].command' "$hj")"
  jq -cn --arg s "$2" --arg p "$1/.claude/projects/p/tr.jsonl" --arg m "$OFFER" '{session_id:$s,transcript_path:$p,stop_hook_active:false,last_assistant_message:$m}' |
    HOME="$1" NO_QUIT_JUDGE=phrases CLAUDE_PLUGIN_ROOT="$here" PATH="$(dirname "$py"):$PATH" sh -c "$cmd"; }
istop() {  # the same stop through install.sh's copy
  jq -cn --arg s "$2" --arg p "$1/.claude/projects/p/tr.jsonl" --arg m "$OFFER" '{session_id:$s,transcript_path:$p,stop_hook_active:false,last_assistant_message:$m}' |
    HOME="$1" NO_QUIT_JUDGE=phrases "$py" -I "$1/.claude/realstop/no-quit.py"; }
isblock() { case "$1" in (*'"block"'*) echo block ;; (*) echo nonblock ;; esac; }
a="$(pstop "$h" dup1)"; b="$(istop "$h" dup1)"
check "plugin: plugin copy first, install.sh copy second: one block, the second backs off" "block silent" "$(isblock "$a") $([ -z "$b" ] && echo silent || echo spoke)"
a="$(istop "$h" dup2)"; b="$(pstop "$h" dup2)"
check "plugin: install.sh copy first, plugin copy second: one block, the second backs off" "block silent" "$(isblock "$a") $([ -z "$b" ] && echo silent || echo spoke)"
pq="$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].command' "$hj")"
c1="$(jq -cn '{session_id:"dc",prompt:"no, that is wrong, do this yourself"}' | HOME="$h" CLAUDE_PLUGIN_ROOT="$here" PATH="$(dirname "$py"):$PATH" sh -c "$pq")"
c2="$(jq -cn '{session_id:"dc",prompt:"no, that is wrong, do this yourself"}' | HOME="$h" "$py" -I "$h/.claude/realstop/correction.py")"
check "plugin: correction hook fires once across a plugin copy and install.sh's copy" "spoke silent" "$([ -n "$c1" ] && echo spoke || echo silent) $([ -z "$c2" ] && echo silent || echo spoke)"

# malformed settings: refuse, never overwrite
for bad in '{"hooks": ' '[1,2]' '{"hooks": []}' '{"hooks":{"Stop":{"x":1}}}' ''; do
  h="$(newhome m)"; printf '%s' "$bad" > "$h/.claude/settings.json"; before="$(shasum "$h/.claude/settings.json")"
  o="$(inst "$h" --judge phrases)"; rc=$?
  check "malformed settings ($bad): install refuses, file untouched, no backup, nothing installed" "1 $before 0 no" "$([ $rc -ne 0 ] && echo 1) $(shasum "$h/.claude/settings.json") $(ls "$h/.claude" | grep -c bak-realstop) $(test -e "$h/.claude/realstop/no-quit.py" && echo yes || echo no)"
done
check "malformed settings: the message explains and says nothing was changed" "yes yes" "$(has "$o" "RealStop:") $(has "$o" "nothing was changed")"
h="$(newhome m2)"; echo '{"x": ' > "$h/.claude/settings.json"; mkdir -p "$h/.claude/realstop"; echo keep > "$h/.claude/realstop/config.json"
inst "$h" --uninstall >/dev/null; rc=$?
check "malformed settings: --uninstall refuses and removes nothing" "1 keep" "$([ $rc -ne 0 ] && echo 1) $(cat "$h/.claude/realstop/config.json")"
# interrupted write
h="$(newhome i)"; echo "$SEED" > "$h/.claude/settings.json"; before="$(shasum "$h/.claude/settings.json")"
HOME="$h" PATH="$t/fakebin:$PATH" REALSTOP_TEST_CRASH_BEFORE_RENAME=1 bash "$here/install.sh" --judge phrases </dev/null >/dev/null 2>&1; rc=$?
check "interrupted write: the old settings.json is intact and still valid" "$before yes" "$(shasum "$h/.claude/settings.json") $(jq -e . "$h/.claude/settings.json" >/dev/null 2>&1 && echo yes || echo no)"
check "interrupted write: the killed run left a temp file, and the exit was nonzero" "1 yes" "$(ls -a "$h/.claude" | grep -c 'realstop-tmp') $([ $rc -ne 0 ] && echo yes)"
inst "$h" --judge phrases >/dev/null
check "interrupted write: the next run cleans up and completes" "4 0" "$(cnt "$h/.claude/settings.json") $(ls -a "$h/.claude" | grep -c 'realstop-tmp')"
# concurrent installers
h="$(newhome c)"; echo "$SEED" > "$h/.claude/settings.json"
for i in 1 2 3 4 5 6 7 8; do "$py" -I "$here/scripts/settings_edit.py" register --settings "$h/.claude/settings.json" --dest "$h/.claude/realstop" >/dev/null 2>&1 & done; wait
check "concurrent: 8 parallel registrations leave valid JSON with one set and the other keys" "4 opus" "$(cnt "$h/.claude/settings.json") $(jq -r .model "$h/.claude/settings.json")"
# uninstall
h="$(newhome u)"; echo "$SEED" > "$h/.claude/settings.json"; mkdir -p "$h/.claude/hooks" "$h/.claude/skills/other" "$h/Library/LaunchAgents"
echo canary > "$h/.claude/hooks/other.sh"; echo canary > "$h/.claude/skills/other/SKILL.md"; echo agent > "$h/Library/LaunchAgents/com.realstop.model-server.plist"
inst "$h" --judge phrases >/dev/null; echo "mine" >> "$h/.claude/realstop/standing.txt"; mkdir -p "$h/.claude/realstop/state"; echo x > "$h/.claude/realstop/state/s.json"
inst "$h" --uninstall >/dev/null; rc=$?
check "uninstall: exits 0 and settings.json is back to the original content" "0 $(echo "$SEED" | jq -S .)" "$rc $(jq -S . "$h/.claude/settings.json")"
check "uninstall: RealStop files, skill file and LaunchAgent are gone; standing.txt is kept" "no no no no yes" "$(test -e "$h/.claude/realstop/no-quit.py" && echo yes || echo no) $(test -e "$h/.claude/realstop/state" && echo yes || echo no) $(test -e "$h/.claude/skills/realstop" && echo yes || echo no) $(test -e "$h/Library/LaunchAgents/com.realstop.model-server.plist" && echo yes || echo no) $(test -e "$h/.claude/realstop/standing.txt" && echo yes || echo no)"
check "uninstall: shared folders and other tools' files are untouched" "canary canary yes" "$(cat "$h/.claude/hooks/other.sh") $(cat "$h/.claude/skills/other/SKILL.md") $(test -d "$h/.claude" && echo yes)"
inst "$h" --uninstall >/dev/null
check "uninstall: a second run is a clean no-op" 0 "$?"
h="$(newhome u2)"; mkdir -p "$h/real"; ln -s "$h/real" "$h/.claude/realstop"; echo precious > "$h/real/file"
inst "$h" --uninstall >/dev/null
check "uninstall: a symlinked ~/.claude/realstop is never emptied" precious "$(cat "$h/real/file")"

# ---- 4. diagnostics ----
h="$(newhome d)"; echo '{"model":"opus","env":{"X":"SETTINGSCANARY"}}' > "$h/.claude/settings.json"; inst "$h" --judge phrases >/dev/null
hsave="$h"
# migration from an earlier build (working name "resilient", installed in ~/.claude/resilient)
mkold() {  # mkold <name>: a home laid out the way the earlier build left it, with the owner's own files and a foreign hook
  h="$(newhome "$1")"; echo "$SEED" > "$h/.claude/settings.json"; inst "$h" --judge phrases >/dev/null
  mv "$h/.claude/realstop" "$h/.claude/resilient"; mv "$h/.claude/skills/realstop" "$h/.claude/skills/resilient"
  sed -i '' 's#^name: realstop#name: resilient#' "$h/.claude/skills/resilient/SKILL.md"
  sed -i '' 's#/\.claude/realstop/#/.claude/resilient/#g' "$h/.claude/settings.json"
  jq --arg m "$h/.claude/resilient/model/realstop-quit" '.model_dir = $m' "$h/.claude/resilient/config.json" > "$h/c" && mv "$h/c" "$h/.claude/resilient/config.json"
  printf '# mine\nmy old approval\n' > "$h/.claude/resilient/standing.txt"; echo '{"at":1,"session":"keepme"}' > "$h/.claude/resilient/stops.jsonl"
  rm -f "$h/.claude/resilient/manifest.json"; echo "$h"
}
h="$(mkold mg)"; S="$h/.claude/settings.json"
check "migration setup: the old layout has four old-path entries and no new folder" "4 no" "$(grep -c '/\.claude/resilient/' "$S" | tr -d ' ') $(test -e "$h/.claude/realstop" && echo yes || echo no)"
o="$(inst "$h")"; rc=$?
check "migration: install exits 0 and prints one line saying what moved" "0 1" "$rc $(printf '%s\n' "$o" | grep -c 'migrated your earlier install')"
check "migration: the old folders are gone, the new ones exist" "no no yes yes" "$(test -e "$h/.claude/resilient" && echo yes || echo no) $(test -e "$h/.claude/skills/resilient" && echo yes || echo no) $(test -d "$h/.claude/realstop" && echo yes || echo no) $(test -f "$h/.claude/skills/realstop/SKILL.md" && echo yes || echo no)"
check "migration: standing.txt content survives untouched" "# mine|my old approval" "$(cat "$h/.claude/realstop/standing.txt" | tr '\n' '|' | sed 's/|$//')"
check "migration: logs and config move, config paths follow" "keepme phrases $h/.claude/realstop/model/realstop-quit" "$(jq -r .session "$h/.claude/realstop/stops.jsonl") $(jq -r .judge "$h/.claude/realstop/config.json") $(jq -r .model_dir "$h/.claude/realstop/config.json")"
check "migration: settings.json ends with only new-path RealStop entries" "4 0" "$(cnt "$S") $(grep -c '/\.claude/resilient/' "$S" | tr -d ' ')"
check "migration: a non-RealStop hook entry is untouched" "yes yes opus" "$(jq -e '.hooks.Stop[] | select(.hooks[0].command == "echo other-stop")' "$S" >/dev/null && echo yes || echo no) $(jq -e '.hooks.SessionStart[] | select(.hooks[0].command == "/opt/x/learn.py")' "$S" >/dev/null && echo yes || echo no) $(jq -r .model "$S")"
check "migration: the migrated install passes its own tamper manifest" "pass" "$(HOME="$h" NO_QUIT_JUDGE=phrases "$py" -I "$h/.claude/realstop/no-quit.py" </dev/null >/dev/null 2>&1; test -e "$h/.claude/realstop/errors.jsonl" && grep -q tamper "$h/.claude/realstop/errors.jsonl" && echo tamper || echo pass)"
sum1="$(shasum "$S")"; o2="$(inst "$h")"
check "migration: a second install migrates nothing and changes nothing" "0 $sum1" "$(printf '%s\n' "$o2" | grep -c 'migrated') $(shasum "$S")"
# both folders present: nothing is moved or overwritten, standing.txt stays as the owner left it
h="$(mkold mg2)"; mkdir -p "$h/.claude/realstop"; echo 'new standing' > "$h/.claude/realstop/standing.txt"
o="$(inst "$h")"
check "migration: an existing ~/.claude/realstop is never overwritten" "new standing 0 my old approval" "$(cat "$h/.claude/realstop/standing.txt") $(printf '%s\n' "$o" | grep -c 'migrated') $(sed -n 2p "$h/.claude/resilient/standing.txt")"
# --pause on an earlier-build install migrates first, then pauses
h="$(mkold mg3)"; inst "$h" --pause >/dev/null
check "migration: --pause on an earlier build migrates and pauses" "no true" "$(test -e "$h/.claude/resilient" && echo yes || echo no) $(jq -r .paused "$h/.claude/realstop/control.json")"
# an invalid settings.json: nothing moves
h="$(mkold mg4)"; echo '{"x": ' > "$h/.claude/settings.json"; inst "$h" >/dev/null; rc=$?
check "migration: an invalid settings.json refuses and moves nothing" "3 yes no" "$rc $(test -d "$h/.claude/resilient" && echo yes || echo no) $(test -e "$h/.claude/realstop" && echo yes || echo no)"
# --uninstall on a not-yet-migrated install removes the old entries and files (standing.txt kept)
h="$(mkold mg5)"; inst "$h" --uninstall >/dev/null; rc=$?
check "migration: --uninstall cleans the old locations too" "0 $(echo "$SEED" | jq -S .) no yes" "$rc $(jq -S . "$h/.claude/settings.json") $(test -e "$h/.claude/skills/resilient" && echo yes || echo no) $(test -e "$h/.claude/resilient/standing.txt" && echo yes || echo no)"
check "migration: --uninstall leaves nothing else in the old folder" "standing.txt" "$(ls -A "$h/.claude/resilient" | tr '\n' ' ' | sed 's/ $//')"
h="$hsave"

echo '{"at":1793000000,"session":"SESSIONCANARY","judge":"phrases","hits":["give-up words","handed the owner a step: run SECRETCMD"],"block":true,"tail":"REPLYCANARY","path":"rules"}' >> "$h/.claude/realstop/stops.jsonl"
d="$(HOME="$h" bash "$here/install.sh" --diagnose 2>&1)"
check "diagnose: prints version, judge, hook status and the decision" "yes yes yes yes" "$(has "$d" "version:") $(has "$d" "judge=phrases") $(has "$d" "Stop=1") $(has "$d" "blocked=yes")"
check "diagnose: leaks no reply text, session id, settings contents or home path" "no no no no no" "$(has "$d" REPLYCANARY) $(has "$d" SESSIONCANARY) $(has "$d" SETTINGSCANARY) $(has "$d" SECRETCMD) $(has "$d" "$h")"
check "diagnose: changes nothing (no new files)" "$(ls -R "$h" | wc -l | tr -d ' ')" "$(HOME="$h" bash "$here/install.sh" --diagnose >/dev/null 2>&1; ls -R "$h" | wc -l | tr -d ' ')"

# ---- 5. loopback only ----
check "server: scripts bind 127.0.0.1 only" "0 yes" "$(grep -c '0\.0\.0\.0' "$here/scripts/realstop-serve.sh") $(has "$(grep -n 'bind\|--host\|ThreadingHTTPServer' "$here/scripts/realstop-serve.sh")" '127.0.0.1')"
cat > "$t/lb.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("nq", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.loopback("http://127.0.0.1:8765"), m.loopback("http://0.0.0.0:8765"), m.loopback("http://192.168.1.5:8765"), m.loopback("https://evil.example"))
PY
check "hook: refuses any local_url that is not loopback" "True False False False" "$(HOME="$t" "$py" -I "$t/lb.py" "$hooks/no-quit.py")"
exit $fail

#!/bin/bash
# `realstop learn` tests: temp HOME, a fake model folder and fake model server (tests/learn_fake.py), synthetic fixtures.
# The real model is not needed; the real-model path is checked by hand with a running server.
set -uo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
hooks="${HOOKS_DIR:-$here/hooks}"
scripts="${SCRIPTS_DIR:-$here/scripts}"
py=/usr/bin/python3; [ -x "$py" ] || py="$(command -v python3)"
t="$(mktemp -d)"; trap 'kill $fpid 2>/dev/null; rm -rf "$t"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: want $2, got $3"; fail=1
  # under tests/mutants.sh the run is over once the mutant's named test has failed
  if [ -n "${MUTANT_TEST:-}" ]; then case "$1" in ("$MUTANT_TEST"*) exit 1 ;; esac; fi; fi; }
lh="$t/home"; R="$lh/.claude/realstop"; L="$R/learned"; mkdir -p "$R" "$lh/.claude/projects/p"
MD="$t/model"; "$py" "$here/tests/learn_fake.py" model "$MD"; "$py" "$here/tests/learn_fake.py" pairs "$t/pairs.jsonl"
"$py" -I "$here/tests/learn_fake.py" server "$t/port" "$L" "$MD" & fpid=$!
n=0; while [ ! -s "$t/port" ] && [ $n -lt 50 ]; do sleep 0.1; n=$((n+1)); done
port="$(cat "$t/port")"
cfg() { printf '{"judge":"local","local_url":"http://127.0.0.1:%s","model_dir":"%s","short_reply_chars":0%s}' "$port" "$MD" "${1:-}" > "$R/config.json"; }
cfg
E_="env -u CLAUDECODE -u CLAUDE_PID -u FACTORY_SEAT_PID"
learn() {  # learn <typed answers> args...: the CLI on a real pty, as a person at a terminal
  ans="$1"; shift
  PTY_INPUT="$ans" $E_ HOME="$lh" REALSTOP_REGRESSION_PAIRS="$t/pairs.jsonl" "$py" "$here/tests/pty_run.py" "$py" -I "$scripts/realstop_learn.py" "$@"; }
plain() { $E_ HOME="$lh" REALSTOP_REGRESSION_PAIRS="$t/pairs.jsonl" "$py" -I "$scripts/realstop_learn.py" "$@" < /dev/null; }
decide() {  # decide <state text> -> "p_shipped p_personal probabilities.yes" from the fake server
  jq -cn --arg s "$1" '{state:$s,question:"q",options:["no","yes"]}' | curl -s --noproxy '*' -H 'Content-Type: application/json' -d @- "http://127.0.0.1:$port/decide" | jq -r '"\(.p_shipped) \(.p_personal) \(.probabilities.yes)"'; }
st() { printf 'request: go\nreply ending: fam=%s score=%s\ntool calls this turn: 3\nbackground work running: no' "$1" "$2"; }

# ---- only a person can confirm ----
check "learn: no TTY: refuses with exit 2" "2" "$(plain; echo $?)"
check "learn: no TTY: the reason is one line naming the TTY" "1 1" "$(plain 2>&1 | grep -c .) $(plain 2>&1 | grep -c 'not a TTY')"
for v in CLAUDECODE CLAUDE_PID FACTORY_SEAT_PID; do
  o="$($E_ "$v=1" HOME="$lh" PTY_INPUT=$'y\n' "$py" "$here/tests/pty_run.py" "$py" -I "$scripts/realstop_learn.py" 2>&1)"; c=$?
  check "learn: agent env $v on a TTY: refuses, exit 2" "2 yes" "$c $(case "$o" in (*"$v is set"*) echo yes ;; (*) echo no ;; esac)"
done
check "learn: --undo and --reset also refuse without a TTY" "2 2" "$(plain --undo e1 2>/dev/null; echo -n $?) $(plain --reset 2>/dev/null; echo $?)"
check "learn: --list and --status work without a TTY" "0 0" "$(plain --list >/dev/null; echo -n $?) $(plain --status > /dev/null; echo $?)"

# ---- the hook: state kept exactly, both heads logged, shift applied ----
mkdir -p "$L"; T="$lh/.claude/projects/p/tr.jsonl"; jq -cn '{type:"user",message:{content:"go"}}' > "$T"
hookrun() {  # hookrun <session> <reply>
  jq -cn --arg s "$1" --arg p "$T" --arg m "$2" '{session_id:$s,transcript_path:$p,stop_hook_active:false,last_assistant_message:$m}' | HOME="$lh" NO_QUIT_JUDGE=local "$py" -I "$hooks/no-quit.py"; }
verdict() { case "$1" in *'"block"'*) echo block ;; *) echo pass ;; esac; }
: > "$R/stops.jsonl"
check "learn: hook: score 0.1 (corrected 0.576) blocks at the shipped 0.5066" block "$(verdict "$(hookrun k1 'fam=H1 score=0.1')")"
sid="$(tail -1 "$R/stops.jsonl" | jq -r .state_id)"
check "learn: hook: the judge state is stored byte for byte under the stop's state_id" "$(st H1 0.1 | sed 's/calls this turn: 3/calls this turn: 0/')" "$(jq -r --arg i "$sid" 'select(.id==$i) | .state' "$L/states.jsonl")"
# the stored state must be exactly what the model saw, even where the log redactor would mask (email, home path, token)
hookrun k1m 'fam=H1 score=0.1 mail ann@example.com, see /Users/someone/proj, token=abcd1234' > /dev/null
sidm="$(tail -1 "$R/stops.jsonl" | jq -r .state_id)"
check "learn: hook: the stored state keeps text the log redactor would mask" "yes" "$(jq -r --arg i "$sidm" 'select(.id==$i) | .state' "$L/states.jsonl" | grep -qF 'ann@example.com, see /Users/someone/proj, token=abcd1234' && echo yes || echo no)"
check "learn: hook: states.jsonl is mode 0600" "600" "$(stat -f %Lp "$L/states.jsonl" 2>/dev/null || stat -c %a "$L/states.jsonl")"
check "learn: hook: both heads are logged (no personal head: null)" "true true true" "$(tail -1 "$R/stops.jsonl" | jq -r '"\(.dt.shipped != null) \(.dt.personal == null) \(.dt.shift == 0)"')"
echo '{"moderate":0.10}' > "$L/shift.json"
check "learn: hook: a +0.10 shift (bar 0.6066) lets corrected 0.576 pass" pass "$(verdict "$(hookrun k2 'fam=H1 score=0.1')")"
check "learn: hook: the shift is logged" "0.1" "$(tail -1 "$R/stops.jsonl" | jq -r .dt.shift)"
echo '{"moderate":-0.5}' > "$L/shift.json"
check "learn: hook: a -0.5 shift is clamped to -0.10 (bar 0.4066): corrected 0.38 passes" pass "$(verdict "$(hookrun k3 'fam=H1 score=0.0145')")"
echo '{"moderate":NaN}' > "$L/shift.json"
check "learn: hook: a broken shift file moves nothing" block "$(verdict "$(hookrun k4 'fam=H1 score=0.1')")"
rm -f "$L/shift.json"
jq -cn --arg p "$T" '{session_id:"k5",transcript_path:$p,stop_hook_active:false,last_assistant_message:"Want me to push it?"}' | HOME="$lh" NO_QUIT_JUDGE=phrases "$py" -I "$hooks/no-quit.py" >/dev/null
check "learn: hook: a stop the rules decided keeps no state" "null" "$(tail -1 "$R/stops.jsonl" | jq -r '.state_id // "null"')"
check "learn: bounds: shifted() stays within 0.10, the floor and 0.99" "0.4066 0.2264 0.99 0.5266" "$("$py" -I - "$hooks/no-quit.py" <<'PY'
import importlib.util, sys
s = importlib.util.spec_from_file_location("h", sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(round(m.shifted(0.5066, -0.5), 4), round(m.shifted(0.2264, -0.1), 4), round(m.shifted(0.95, 0.1), 4), round(m.shifted(0.5066, 0.02), 4))
PY
)"

# ---- the head math ----
check "learn: refit with zero examples is the shipped head bit for bit; the pull shrinks the change; a head file round-trips" "True True True" "$("$py" -I - "$scripts" "$MD" <<'PY'
import sys; sys.path.insert(0, sys.argv[1])
import realstop_learn as rl, os
w, b, _ = rl.read_head(os.path.join(sys.argv[2], "head.safetensors"))
d = [0.0] * 1024; d[3] = 5.0; d[9] = -3.0
z = rl.fit_delta(w, [], 0.25)
n = lambda x: sum(a * a for a in x) ** .5
big, small = rl.fit_delta(w, [(d, 1)], 0.25), rl.fit_delta(w, [(d, 1)], 0.05)
w2, b2, meta = rl.read_head(__import__("tempfile").mktemp())if False else (None, None, None)
data = rl.head_bytes(w, b, {"model": "x"}); p = sys.argv[2] + "/rt.safetensors"; open(p, "wb").write(data)
w3, b3, m3 = rl.read_head(p)
print([x + y for x, y in zip(w, z)] == w, n(big) < n(small), w3 == w and b3 == b and m3 == {"model": "x"})
PY
)"

# ---- review flow ----
NOW=$(date +%s)
mkstop() {  # mkstop <session> <at> <judge> <block> <state id|-> <fam> <score> <adjusted> [jev quit]
  jq -cn --arg s "$1" --argjson at "$2" --arg j "$3" --argjson b "$4" --arg sid "$5" --arg fam "$6" --argjson adj "$8" --arg jq "${9:-}" \
    '{at:$at,session:$s,judge:$j,hits:[],block:$b,tail:("ending of "+$fam+". Want me to push it?"),
      dt:(if $j=="local" then {raw:0.5,adjusted:$adj,threshold:0.5066,threshold_with_hit:0.3363,strength:"moderate",shipped:$adj,personal:null,shift:0} else null end),
      jev:(if $j=="jev" then {quit:($jq|tonumber),gate:0.1,done:0.2} else null end),
      state_id:(if $sid=="-" then null else $sid end)}' >> "$R/stops.jsonl"
  [ "$5" = "-" ] || jq -cn --arg i "$5" --argjson at "$2" --arg s "$1" --arg st "$(st "$6" "$7")" '{id:$i,at:$at,session:$s,state:$st}' >> "$L/states.jsonl"; }
mkfix() {  # mkfix <session> <at> [false_block]
  if [ "${3:-}" = false_block ]; then jq -cn --arg s "$1" --argjson at "$2" '{at:$at,session:$s,kind:"false_block",said:"it was right to stop",reply_tail:"t"}' >> "$R/corrections.jsonl"
  else jq -cn --arg s "$1" --argjson at "$2" '{at:$at,session:$s,said:"no, do it yourself",reply_tail:"t"}' >> "$R/corrections.jsonl"; fi; }
reset_all() { rm -rf "$L"; mkdir -p "$L"; : > "$R/stops.jsonl"; : > "$R/corrections.jsonl"; rm -f "$R/learned.json"; cfg; }
reset_all
mkstop sA $((NOW-100)) local false a1 ZA 0.0 0.35        # let through, a miss: QUIT proposed
mkstop sB $((NOW-200)) local true a2 ZB 0.15 0.69        # blocked, owner pushed back: RIGHT STOP proposed
mkstop sC $((NOW-300)) jev false - ZC 0.0 0 0.3          # the cloud judge let it through: QUIT proposed
mkstop sD $((NOW-400)) phrases false - ZD 0.0 0           # rules alone: cannot be learned from
mkfix sA $((NOW-90)); mkfix sB $((NOW-190)) false_block; mkfix sC $((NOW-290)); mkfix sD $((NOW-390))
check "learn: status: 3 pending, 1 not learnable, nothing stored" "pending corrections: 3 (1 more cannot be learned from: decided by rules alone)" "$(plain --status | head -1)"
check "learn: installer note names the count, not the agent" "RealStop: 3 corrections to review. Run: realstop learn" "$(plain --installer-note)"
before="$(decide "$(st ZA 0.0)")"
learn $'q\n' > "$t/q.out"
check "learn: q exits without changes" "none, no head, pending 3" "$([ -s "$L/examples.jsonl" ] && echo has || echo none), $([ -e "$L/head.safetensors" ] && echo head || echo no head), pending $(plain --status | sed -n 1p | grep -o ': [0-9]*' | tr -d ': ')"
learn $'y\nn\ns\n' > "$t/rev.out"
check "learn: review shows the proposal, the ending and the score" "yes yes yes" "$(grep -q 'proposed label:  QUIT (should have been blocked)' "$t/rev.out" && echo -n yes) $(grep -q "Want me to push it" "$t/rev.out" && echo -n yes) $(grep -q 'model score:     0.35 (moderate blocks at 0.51' "$t/rev.out" && echo yes)"
check "learn: y stores one example; n discards; s keeps pending" "1 1" "$(wc -l < "$L/examples.jsonl" | tr -d ' ') $(plain --status | sed -n 1p | grep -o ': [0-9]*' | tr -d ': ')"
check "learn: the confirmed example's twin-free near duplicate now scores higher with the personal head" "up" "$(set -- $(decide "$(st ZA 0.0)"); "$py" -c "import sys; print('up' if float(sys.argv[2]) > float(sys.argv[1]) + 0.2 else 'no')" "$1" "$2")"
check "learn: the confirmed stop lands on the QUIT side (corrected p >= 0.5066)" "yes" "$(set -- $(decide "$(st ZA 0.0)"); "$py" -I - "$scripts" "$3" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import realstop_learn as rl
h = rl.hook(); p = {"real_mix": {"no": 0.6526277897768179, "yes": 0.34737221022318215}, "train_mix": {"deep": {"no": 0.5, "yes": 0.5}}, "winner": "deep"}
print("yes" if h.correct(float(sys.argv[2]), p) >= 0.5066 else "no")
PY
)"
check "learn: one confirmed QUIT example does not move its right-stop twin across the threshold (and the gate holds)" "pass" "$(set -- $(decide "$(st ZA -0.6)"); "$py" -I - "$scripts" "$3" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import realstop_learn as rl
h = rl.hook(); p = {"real_mix": {"no": 0.6526277897768179, "yes": 0.34737221022318215}, "train_mix": {"deep": {"no": 0.5, "yes": 0.5}}, "winner": "deep"}
print("block" if h.correct(float(sys.argv[2]), p) >= 0.5066 else "pass")
PY
)"
check "learn: an unrelated stop (another family) moves by under 0.02" "ok" "$(set -- $(decide "$(st ZQ 0.2)"); "$py" -c "import sys; print('ok' if abs(float(sys.argv[1]) - float(sys.argv[2])) < 0.02 else 'moved')" "$1" "$3")"
check "learn: no threshold shift after one confirmation" "no" "$(test -e "$L/shift.json" && echo yes || echo no)"
check "learn: --list shows the example (id, date, label, excerpt)" "e1 QUIT" "$(plain --list | head -1 | awk '{print $1, $3}')"
check "learn: --undo removes it: no personal head, scores back to as-shipped exactly" "same" "$(learn '' --undo e1 > /dev/null; [ ! -e "$L/head.safetensors" ] && [ "$(decide "$(st ZA 0.0)")" = "$before" ] && echo same || echo differ)"
check "learn: an undone item does not come back as pending (only the skipped one is)" "1" "$(plain --status | sed -n 1p | grep -o ': [0-9]*' | tr -d ': ')"

# the cloud judge: y on a missed quit lowers quit_min, y on a wrong block raises it, f then y labels the other way; all replayed
reset_all
mkstop sF $((NOW-350)) jev false - ZF 0.0 0 0.5; mkfix sF $((NOW-340))
mkstop sE $((NOW-250)) jev true - ZE 0.0 0 0.7; mkfix sE $((NOW-240)) false_block
mkstop sC $((NOW-300)) jev false - ZC 0.0 0 0.3; mkfix sC $((NOW-290))
learn $'y\ny\nf\ny\n' > "$t/flip.out"   # newest first: sE right stop (0.65), sC quit (jev 0.3: floor 0.35), sF flipped to a right stop (0.40)
check "learn: cloud judge: three confirmations, quit_min replayed in order from the 0.6 seed" "3 0.4" "$(wc -l < "$L/examples.jsonl" | tr -d ' ') $(jq -r .quit_min "$R/learned.json")"
check "learn: the flipped example is stored as a right stop" "2" "$(jq -r 'select(.judge=="jev" and .label=="right_stop") | .id' "$L/examples.jsonl" | wc -l | tr -d ' ')"
check "learn: cloud judge examples reach the hook only through learned.json (the quit's tail kept, no vectors stored)" "1 0" "$(jq '.examples | length' "$R/learned.json") $(jq -r 'select(.vector) | .id' "$L/examples.jsonl" | wc -l | tr -d ' ')"
learn '' --undo e3 >/dev/null
check "learn: --undo of a cloud judge example replays learned.json without it" "0.35" "$(jq -r .quit_min "$R/learned.json")"
learn '' --reset >/dev/null
check "learn: --reset removes examples, head, shift and learned.json" "0" "$(ls "$L/examples.jsonl" "$L/head.safetensors" "$L/shift.json" "$R/learned.json" 2>/dev/null | wc -l | tr -d ' ')"
reset_all
mkstop sA $((NOW-100)) local false a1 ZA 0.0 0.35; mkfix sA $((NOW-90))
learn $'n\n' >/dev/null
check "learn: n discards: nothing stored, and it does not come back" "none 0" "$([ -s "$L/examples.jsonl" ] && echo has || echo none) $(plain --status | sed -n 1p | grep -o ': [0-9]*' | tr -d ': ')"

# ---- the regression gate refuses a deliberately mislabeled batch ----
reset_all
for i in 1 2 3 4 5 6; do mkstop "m$i" $((NOW-100*i)) local false "g$i" "ZM$i" 0.9 0.99; mkfix "m$i" $((NOW-100*i+5)); done   # the shipped head blocks these strongly; the owner says "right stop" to all
o="$(learn $'f\ny\nf\ny\nf\ny\nf\ny\nf\ny\nf\ny\n')"
nsaved="$(printf '%s\n' "$o" | grep -c '^Saved as e')"; nref="$(printf '%s\n' "$o" | grep -c '^Not saved:')"
check "learn: a mislabeled batch is refused part-way, with the reason, and the previous head stays" "yes yes" "$([ "$nref" -ge 1 ] && echo -n yes || echo -n no) $([ "$nsaved" -ge 1 ] && [ "$nsaved" -lt 6 ] && echo yes || echo no)"
# two identical stops confirmed with opposite labels: no head can put both on their chosen side, so the side check
# alone must refuse the second (the released-pairs check barely moves for a near-zero change)
reset_all
mkstop d1 $((NOW-200)) local false g1 ZD 0.9 0.99; mkfix d1 $((NOW-195))
mkstop d2 $((NOW-100)) local true g2 ZD 0.9 0.99; mkfix d2 $((NOW-95)) false_block
o1="$(learn $'y\ny\n')"
check "learn: contradictory examples are refused by the side check" "yes" "$(case "$o1" in (*"would still be scored on the wrong side"*) echo yes ;; (*) echo no ;; esac)"
check "learn: the refusal names the check that failed" "yes" "$(case "$o" in (*"wrong side"*|*"released-pairs check"*) echo yes ;; (*) echo no ;; esac)"
check "learn: what is stored equals what was accepted, and the kept head passes the gate (pairs both right not lowered by more than 1 point)" "yes" "$("$py" -I - "$scripts" "$MD" "$L" "$t/pairs.jsonl" "$here/tests" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[5])
import realstop_learn as rl, learn_fake as lf
w = rl.read_head(os.path.join(sys.argv[2], "head.safetensors"))[0]
pw = rl.read_head(os.path.join(sys.argv[3], "head.safetensors"))[0]
pairs = rl.load_pairs(sys.argv[4]); vec = [lf.vec(p["state"]) for p in pairs]
thr = 0.5066; a = rl.metrics(pairs, [rl.dot(w, v) for v in vec], thr, lf.PACK); b = rl.metrics(pairs, [rl.dot(pw, v) for v in vec], thr, lf.PACK)
n = sum(1 for l in open(os.path.join(sys.argv[3], "examples.jsonl")))
print("yes" if b["pairs_right"] >= a["pairs_right"] - 0.01 - 1e-9 and n >= 1 else "no")
PY
)"
check "learn: refit refuses an all-mislabeled batch outright (pull ladder exhausted)" "None" "$("$py" -I - "$scripts" "$MD" "$t/pairs.jsonl" "$here/tests" <<'PY'
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[4])
import realstop_learn as rl, learn_fake as lf
w = rl.read_head(os.path.join(sys.argv[2], "head.safetensors"))[0]
pairs = rl.load_pairs(sys.argv[3]); vec = [lf.vec(p["state"]) for p in pairs]
exs = [(lf.vec("fam=B%d score=0.9" % i), 0) for i in range(12)]
print(rl.refit(w, exs, pairs, vec, 0.5066, lf.PACK)[0])
PY
)"

# ---- threshold shift: none before the margin, then 0.02 per step, undoable ----
reset_all
for i in 1 2 3 4 5; do mkstop "s$i" $((NOW-100*i)) local false "z$i" "Z$i" 0.0 0.35; mkfix "s$i" $((NOW-100*i+5)); done
learn $'y\ny\ny\n' >/dev/null
check "learn: 3 more missed quits than wrong blocks: no shift yet" "no" "$(test -e "$L/shift.json" && echo yes || echo no)"
learn $'y\n' >/dev/null
check "learn: the 4th moves the moderate bar by -0.02" '{"moderate":-0.02}' "$(jq -c . "$L/shift.json" 2>/dev/null)"
ids="$(jq -r .id "$L/examples.jsonl" | tail -1)"
learn '' --undo "$ids" >/dev/null
check "learn: --undo reverts the shift" "no" "$(test -e "$L/shift.json" && echo yes || echo no)"
check "learn: the shift never exceeds 0.10 in total (40 missed quits)" "-0.1" "$("$py" -I - "$scripts" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import realstop_learn as rl
ex = [{"judge": "local", "label": "quit", "strength": "moderate", "created": i} for i in range(40)]
print(rl.shift_for(ex)[0]["moderate"])
PY
)"
check "learn: a wrong block and a missed quit cancel: no shift from 4+4" "{}" "$("$py" -I - "$scripts" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import realstop_learn as rl
ex = [{"judge": "local", "label": l, "strength": "moderate", "created": i} for i, l in enumerate(["quit", "right_stop"] * 4)]  # 4 each: a count of 4 in one direction alone would shift
print(rl.shift_for(ex)[0])
PY
)"

# ---- reset restores as-shipped scores exactly ----
reset_all
mkstop sA $((NOW-100)) local false a1 ZA 0.0 0.35; mkfix sA $((NOW-90))
before="$(decide "$(st ZA 0.0)")"; learn $'y\n' >/dev/null
check "learn: a head is in use after a confirmation (personal differs from shipped)" "differs" "$([ "$(decide "$(st ZA 0.0)")" != "$before" ] && echo differs || echo same)"
learn '' --reset >/dev/null
check "learn: --reset: scores are exactly as shipped" "$before" "$(decide "$(st ZA 0.0)")"
check "learn: fresh install, zero examples: personal is null and probabilities are the shipped ones" "null" "$(decide "$(st ZA 0.0)" | awk '{print $2}')"
check "learn: phrase-rules-only users are told learning needs the local model or the cloud judge" "yes" "$(printf '{"judge":"phrases"}' > "$R/config.json"; learn '' | grep -q 'needs the local model or the cloud decision model' && echo yes || echo no)"
exit $fail

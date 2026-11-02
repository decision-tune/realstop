#!/bin/bash
# Mutation tests for the guards: copy hooks to a temp dir, re-introduce ONE bug per mutant with an exact-anchor patch
# (anchor missing or ambiguous = loud FAIL), run tests/run.sh against the copy, and require the named test to fail.
#   bash tests/mutants.sh
#   SHARD=1/3 bash tests/mutants.sh        one of three parallel shards
#   ANCHORS_ONLY=1 bash tests/mutants.sh   only check that every anchor matches once and every mutant compiles
set -uo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
py=/usr/bin/python3; [ -x "$py" ] || py="$(command -v python3)"
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
fail=0; killed=0; total=0; idx=0

mutant() {  # mutant <name> <file> <anchor> <replacement> <test that must fail>
  idx=$((idx+1))  # SHARD=k/N runs every Nth mutant, starting at k (parallel shards, each with its own temp dir)
  case "$1" in (*"${MUTANT_FILTER:-}"*) ;; (*) return ;; esac  # MUTANT_FILTER=text runs only mutants whose name has it
  if [ -n "${SHARD:-}" ] && [ $(( (idx - 1) % ${SHARD#*/} )) -ne $(( ${SHARD%/*} - 1 )) ]; then return; fi
  total=$((total+1))
  rm -rf "$t/hooks" "$t/scripts"; mkdir "$t/hooks" "$t/scripts"; cp "$here"/hooks/*.py "$t/hooks/"; cp "$here"/scripts/*.py "$t/scripts/"
  target="$t/hooks/$2"; case "$2" in (scripts/*) target="$t/$2" ;; esac  # a mutant of the realstop learn code names scripts/realstop_learn.py
  if ! "$py" - "$target" "$3" "$4" <<'PY'
import sys
p, anchor, repl = sys.argv[1:4]
s = open(p).read()
if s.count(anchor) != 1:
    sys.exit("anchor found %d times: %s" % (s.count(anchor), anchor))
open(p, "w").write(s.replace(anchor, repl))
PY
  then echo "FAIL mutant '$1': anchor problem"; fail=1; return; fi
  if ! "$py" -m py_compile "$target" 2>/dev/null; then echo "FAIL mutant '$1': mutated file does not compile"; fail=1; return; fi
  if [ "${ANCHORS_ONLY:-}" = 1 ]; then echo "ANCHOR OK $1"; killed=$((killed+1)); return; fi  # seconds: check anchors first
  runner=run.sh; case "$5" in (bypass:*|gentle:*|"dedupe: a second registration"*) runner=safety.sh ;; esac  # the safety tests live in their own script
  case "$5" in (learn:*) runner=learn.sh ;; esac  # the realstop learn tests live in their own script
  out="$(MUTANT_TEST="$5" HOOKS_DIR="$t/hooks" SCRIPTS_DIR="$t/scripts" /bin/bash "$here/tests/$runner" 2>&1)"
  if printf '%s\n' "$out" | grep -qF "FAIL $5"; then echo "KILLED $1 (by: $5)"; killed=$((killed+1))
  else echo "FAIL mutant '$1' survived: no '$5' failure"; fail=1; fi
}

mutant "question examples: quoted examples not filtered" no-quit.py '    text = without_question_examples(text)' '    text = text' "question examples: quoted question passes"
mutant "question examples: table examples not filtered" no-quit.py '    text = without_question_examples(text)' '    text = text' "question examples: table question passes"
mutant "question examples: code examples not filtered" no-quit.py '    text = without_question_examples(text)' '    text = text' "question examples: code question passes"
mutant "question examples: real asks discarded" no-quit.py '    text = without_question_examples(text)' '    text = ""' "question examples: real question after quote blocks"
mutant "errors.jsonl cap dropped" no-quit.py 'if os.path.getsize(path) > ERR_MAX_BYTES:' 'if False:' "errors.jsonl is capped at the newest 1000 lines (+1 new, then it grows again)"
mutant "drop USER_STOP check" no-quit.py 'if not sub and USER_STOP.search(strip_quoted' 'if False and USER_STOP.search(strip_quoted' "live stop is obeyed"
mutant "drop denial-record pass-through" no-quit.py '    if errors and denial_scoped(body, text, errors, n, extra):' '    if False:' "denial scope: a named denial with nothing else left is excused before any judge"
mutant "a denial excuses the whole reply" no-quit.py '    return not quit_hits(strip_quoted("\n".join(rest)), n or any(token_line(x) for x in ss), extra)' '    return True' "bracketed denial with an offer blocks"
mutant "handed denied command not scoped" no-quit.py 'and denial_scoped(body, text, recs, n, extra):' 'and denial_named(body, text, recs):' "denial scope: a handed denied command plus a deferral blocks"
mutant "voided denial goes to the judge" no-quit.py '    elif dvoid:  # decided by the rule: no judge call' '    elif False:' "denial scope: live repro, decided by the rule, no judge call"
mutant "voided denial nudge drops the denied command" no-quit.py 'step = dstep or gated_step(text)' 'step = gated_step(text)' "denial scope: live repro gets the gate rule first"
mutant "drop conditional deferral phrase" no-quit.py 'r"|(if|once|when|after|as soon as) you(' 'r"|(?!x)x(if|once|when|after|as soon as) you(' "conditional deferral fires: Once you allow the run"
mutant "conditional deferral: rerunning the gated step not exempt" no-quit.py '(?!(re-?)?(run|try|retry|execute)\b' '(?!(re-?)?(xrun|try|retry|execute)\b' "conditional deferral near miss passes: If you approve pytest, I'll run it."
mutant "drop block cap" no-quit.py 'if kind and stateful and st["turn_blocks"] >= effective_cap(cfg):' 'if False:' "cap at 5 blocks per turn"
mutant "completed background tasks count as live" no-quit.py 'not in ("completed", "failed", "done", "killed")' 'not in ()' "finished background work does not excuse a judged quit"
mutant "drop plan-mode skip" no-quit.py 'or payload.get("permission_mode") == "plan"' 'or False' "plan mode is skipped"
mutant "ignore held-after-nudge" no-quit.py 'if not stateful or n <= st["at_tools"]:' 'if not stateful:' "held after nudge: no tool call since, stop passes"
mutant "subagent excused by background work" no-quit.py 'if kind and (not hits or hits == ["announced, not done"]) and not sub and armed(payload):' 'if kind and (not hits or hits == ["announced, not done"]) and armed(payload):' "armed work does not excuse a subagent's stop"
mutant "correction reads quoted text" correction.py 'prompt = own_words(raw)' 'prompt = raw' "a quoted reply with wrong-nudge is not a correction"
mutant "drop teach-after-a-miss phrases" correction.py '|\b(doesn'"'"'?t|doens'"'"'?t|does not|won'"'"'?t|never) happen again\b' '' "correction: make sure this never happens again"
mutant "roadmap status counts as a give-up" no-quit.py '    s = roadmap_status(s)
' '' "regression synthetic-4, rules: What the product does not claim:"
mutant "give-up words count after tool calls" no-quit.py '([GIVE_UP] if not n else [])' '[GIVE_UP]' "give-up words after real tool calls pass"
mutant "drop unearned-caution check" no-quit.py '        if WAKE_PLAN.search(t) or CAUTION_EVIDENCE.search(" ".join(ss[i:i + 2])):' '        if True:' "caution: a one-week rollback window blocks"
mutant "caution evidence ignored" no-quit.py '        if WAKE_PLAN.search(t) or CAUTION_EVIDENCE.search(" ".join(ss[i:i + 2])):' '        if WAKE_PLAN.search(t):' "caution with a cited file and line passes"
mutant "local judge drops the training option order" no-quit.py ', "options": ["no", "yes"]}).encode())' '}).encode())' "armed work does not excuse a subagent's stop"
mutant "drop empty agent_type skip" no-quit.py '(sub and not payload.get("agent_type")) or ' '' "SubagentStop with empty agent_type is skipped"
mutant "drop transcript confinement" no-quit.py 'return real if real.startswith(root + os.sep) else ""' 'return real' "transcript outside the root is ignored"
mutant "drop dedupe" no-quit.py 'if duplicate(key, hashlib.sha1((text + "|%d" % len(tools)).encode()).hexdigest()[:12]):' 'if False:' "dedupe: first block, duplicate silent"
mutant "keep injected user text" no-quit.py 'body = " ".join(command_turn(x) or owner_text(x) for x in parts)' 'body = " ".join(parts)' "harness-injected text is skipped for the live-stop check"
mutant "drop announce rule" no-quit.py 'if ANNOUNCE.search(' 'if False and ANNOUNCE.search(' "announced, no tool call blocks"
mutant "scope check ignores its flag" no-quit.py 'and cfg["scope_check"] and p' 'and p' "scope check is off by default"
mutant "scope check repeats" no-quit.py 'and not st["scoped"]' '' "scope check does not repeat"
mutant "shadow still blocks" no-quit.py '    if shadow:
        kind = ""' '    pass' "shadow env never blocks"
mutant "false_block ignores the 15-minute window" correction.py 'time.time() - last.get("at", 0) <= BLOCK_WINDOW_S' 'True' "a block older than 15 minutes is not a false_block"
mutant "cap counts runs, not turns" no-quit.py 'st["turn_blocks"] >= effective_cap(cfg)' 'st["blocks"] >= effective_cap(cfg)' "cap at 5 blocks per turn, and later stops in the same turn stay allowed"
mutant "allowed stop keeps the nudge count" no-quit.py 'st["blocks"] = 0  # an allowed stop' 'pass  # an allowed stop' "after an allowed stop the next nudge is the full first paragraph"
mutant "tool count survives a new user message" no-quit.py 'user, n, texts, tools, errors, bg_turn, denied = body, n + 1, [], [], [], False, []' 'user, n, texts, errors, bg_turn, denied = body, n + 1, [], [], False, []' "tools restart at a new user message (announce rule fires)"
mutant "slash command with args is harness text" no-quit.py 'command_turn(x) or owner_text(x) for x in parts' 'owner_text(x) for x in parts' "a slash command with args replaces the old request"
mutant "subagent brief checked for a live stop" no-quit.py 'if not sub and USER_STOP' 'if USER_STOP' "a subagent brief saying stop is still judged"
mutant "subagents keyed by agent_type" no-quit.py '(hashlib.sha1(tpath.encode()).hexdigest()[:12] if tpath else "")' 'str(payload.get("agent_type") or "")' "parallel subagents without agent_id: each is held after its own nudge"
mutant "dedupe lock not exclusive" no-quit.py 'os.O_CREAT | os.O_EXCL | os.O_WRONLY' 'os.O_CREAT | os.O_WRONLY' "parallel copies: exactly one block line"
mutant "false_block beats a correction" correction.py 'and not CORRECTION.search(re.sub(r"^\s*no\b,?", "", prompt[:600]))):' '):' "correction not a false_block, context injected: That's fine as a draft, but you skipped the tests. Do it yourself."
mutant "held stop not logged" no-quit.py 'log({"session": sid, "judge": "none", "hits": [], "block": False, "held": True, "tools": n, "qset": QSET})' 'pass' "a held stop is logged as block:false"

# ---- adversarial-review guards (2026-10-30) ----
mutant "BLOCKED: claim excuses again" no-quit.py 'kind = "quit" if not gate and (handoff or dvoid or decide(hits, p, cfg)) else ""' 'kind = "quit" if not gate and not GATE_TOKEN.search(body) and (handoff or dvoid or decide(hits, p, cfg)) else ""' "review3: an offer with an empty BLOCKED: line blocks"
mutant "reply denial wording excuses again" no-quit.py 'kind = "quit" if not gate and (handoff or dvoid or decide(hits, p, cfg)) else ""' 'kind = "quit" if not gate and not DENIAL.search(text) and (handoff or dvoid or decide(hits, p, cfg)) else ""' "review3: permission denied in a code block with no tool error blocks"
mutant "reply EVIDENCE words excuse again" no-quit.py 'kind = "quit" if not gate and (handoff or dvoid or decide(hits, p, cfg)) else ""' 'kind = "quit" if not gate and not evidence and (handoff or dvoid or decide(hits, p, cfg)) else ""' "review14: Exit code 0 with an offer blocks"
mutant "generic bracket denial back" no-quit.py '|\[(CI Bypass|Data Exfiltration|Credential [A-Z]\w+)\]", re.I)' '|\[(CI Bypass|Data Exfiltration|Credential [A-Z]\w+|[A-Z][a-z]+ [A-Z][a-z]+)\]", re.I)' "judged: a real denial does not make any bracket a named denial"
mutant "denial record not required" no-quit.py '    if not recs:
        return False' '    if False:
        return False' "judged: a different error is not this one"
mutant "owner approval excuses any ask" no-quit.py 'lambda x: bool(acts & stems(x))' 'lambda x: True' "d3: approval for deleting does not excuse finishing other work"
mutant "drop owner-approval gate" no-quit.py '    if acts and excused(sents, lambda x: bool(acts & stems(x)), n, extra):' '    if False:' "owner approval asked for that step passes"
mutant "drop gated-consent gate" no-quit.py '    if excused(sents, lambda x: bool(gated_step(x)), n, extra):' '    if False:' "gated step with a consent ask passes"
mutant "plain push counts as gated" no-quit.py 'r"|\bdocker push\b|' 'r"|\bpush\b|\bdocker push\b|' "plain push offer still blocks with the normal nudge"
mutant "gated nudge dropped" no-quit.py '    elif step:
        reason = GATED_NUDGE' '    elif False:
        reason = GATED_NUDGE' "gated step without consent gets the gated nudge"
mutant "excused stop is judged" no-quit.py '    if gate:
        path, used, p = "gate", "none", None' '    if False:
        path, used, p = "gate", "none", None' "an excused stop makes no judge call"
mutant "cloud without opt-in" no-quit.py '    if cfg.get("cloud_ok") is not True:' '    if False:' "auto never calls the cloud judge without cloud_ok (env jev too); with it, the cloud judge answers"
mutant "shadow may call the cloud" no-quit.py 'cfg["cloud_ok"] = False  # shadow' 'pass  # shadow' "shadow makes no cloud call even with cloud_ok"
mutant "non-loopback local_url allowed" no-quit.py '    if not loopback(url):' '    if False:' "non-loopback local_url is refused"
mutant "local judge honors http_proxy" no-quit.py 'build_opener(urllib.request.ProxyHandler({}), _NoRedirect)' 'build_opener(_NoRedirect)' "local judge ignores http_proxy"
mutant "local judge follows redirects" no-quit.py 'build_opener(urllib.request.ProxyHandler({}), _NoRedirect)' 'build_opener(urllib.request.ProxyHandler({}))' "local judge refuses a redirect"
mutant "probability range unchecked" no-quit.py '    if not (math.isfinite(v) and 0.0 <= v <= 1.0):' '    if False:' "NaN from the local judge is no answer"
mutant "cloud request redacted again" no-quit.py '"state": {"request": request[-1500:]' '"state": {"request": redact(request)[-1500:]' "the cloud judge state is sent as built, no redaction"
mutant "secret values not redacted" no-quit.py '(\s*[:=]\s*|\s+is\s+)[^\s;,]+",' '(\s*[:=]\s*|\s+is\s+)(?!x)x",' "redact strips password and token values"
mutant "learned.json overrides config" no-quit.py '    lj = _json("learned.json")' '    cfg.update(_json("learned.json")); lj = _json("learned.json")' "learned.json cannot set shadow, judge, cloud_ok, block_cap, local_url"
mutant "learned quit_min unbounded" no-quit.py 'max(min(FLOOR, cfg["quit_min"]), min(max(CEILING, cfg["quit_min"]), q))' 'q' "learned quit_min moves both ways, bounded [0.35, 0.9]"
mutant "learned quit_min only lowers (false_block raise not applied)" no-quit.py 'min(max(CEILING, cfg["quit_min"]), q)' 'min(cfg["quit_min"], q)' "learned quit_min moves both ways, bounded [0.35, 0.9]"
mutant "standing: a descriptive sentence matches" no-quit.py '        if (STANDING_ASK.search(prose) or token_line(s)' '        if (asks(s) or token_line(s)' "regression synthetic-3, rules: Status: the build queue is green"
mutant "tamper ignored" no-quit.py 'cfg = learned(tamper=bool(bad))' 'cfg = learned()' "tamper: config shadow ignored, still blocks"
mutant "hook self-check dropped" no-quit.py '        if sha256(os.path.realpath(__file__)) != files.get("no-quit.py"):' '        if False:' "tamper: edited hook is logged"
mutant "main exception not logged" no-quit.py '        err("main", e)' '        pass' "garbage input logs its exception type, no text"
mutant "correction exception not logged" correction.py '    except Exception as e:
        err(e)' '    except Exception as e:
        pass' "correction logs its exception type"
mutant "null transcript line crashes" no-quit.py '                if not isinstance(d, dict):
                    continue
' '' "a null transcript line is skipped"
mutant "quote-only reply hidden" no-quit.py 'claims = body if len(re.sub(r"\W", "", body)) >= 12 else text' 'claims = body' "review14: a reply that is all quote is judged whole"
mutant "final prose block not scanned" no-quit.py '    if blocks and text.rstrip().endswith("```")' '    if False and blocks' "offer in a final untagged code block blocks"
mutant "final block tag ignored" no-quit.py 'blocks[-1][0].strip().lower() in ("", "text", "txt", "md", "markdown")' 'True' "a final sh block is not read as prose"
mutant "trailing heading hides the announcement" no-quit.py '        lines.pop()  # a trailing heading' '        break  # a trailing heading' "announce before a trailing heading blocks"
mutant "stop with an object obeyed" no-quit.py '\s*(?:$|[.!?,;:\n\u2014\u2013-]|and\b)", re.I | re.M)' '", re.I | re.M)' "d6: wait for CI, I want it green is not a stop"
mutant "stop only in the first 400 characters" no-quit.py 'USER_STOP.search(strip_quoted(user))' 'USER_STOP.search(strip_quoted(user)[:400])' "d6: a stop after 400 characters is obeyed"
mutant "stop anywhere in a sentence" no-quit.py 'r"(?:^|(?<=[.!?;,\n]))\s*(?:(?:no|' 'r"(?:(?:no|' "d6: a negated stop at the end of a sentence is not a stop"
mutant "injected text by substring" no-quit.py 'return "" if s.startswith(INJECTED) else s' 'return "" if any(x in s for x in INJECTED) else s' "d6: an owner message naming <system-reminder> is the owner's turn"
mutant "compact summary read as the owner" no-quit.py 'not any(d.get(k) for k in HARNESS_KEYS)' 'not d.get("isMeta")' "d6: a compact summary record is not the owner's turn"
mutant "gate excuses the whole reply" no-quit.py '    return bool(own) and not quit_hits(' '    return bool(own) or not quit_hits(' "d1: a deferral beside a gated consent sentence blocks, gated nudge"
mutant "gated word anywhere in the reply" no-quit.py 'lambda x: bool(gated_step(x))' 'lambda x: bool(gated_step(text))' "d1: sudo in another sentence does not excuse"
mutant "bare publish and payments gated again" no-quit.py 'r"|\bsudo +\S|' 'r"|\bsudo +\S|\bpublish(ing|ed)?\b|\bpayments?\b|' "d1: the payments module is not a payment"
mutant "command output counts as a denial record" no-quit.py 'recs = [e for e in errors if DENIAL_RECORD.match(e)]' 'recs = [e for e in errors if re.search(r"denied by|classifier|hook error", e)]' "judged: d2: printf-forged classifier denial is not a denial record"
mutant "missing manifest ignored" no-quit.py 'return ["manifest.json"] if os.path.dirname' 'return [] if os.path.dirname' "d5: a deleted manifest is tamper: shadow ignored, still blocks"
mutant "no overall deadline" no-quit.py '        if left() < 1:  # one deadline' '        if False:  # one deadline' "d7: past the deadline no judge is called and it is logged"
mutant "risky step gets the execute nudge" no-quit.py '(risky.group(0) if risky else "")' '""' "d4: destructive step gets the gated nudge: Should I run \`git reset --hard origin/main\`?"
mutant "neutral command nudge dropped" no-quit.py '    elif cmd:' '    elif False:' "d4: an unclassified command gets a neutral nudge"
mutant "armed work no longer excuses an announced wait" no-quit.py '(not hits or hits == ["announced, not done"])' 'not hits' "armed work excuses an announced wait"

# ---- local v3 DecisionTune judge (2026-10-30): correction, dial, fallback, state ----
mutant "real-mix correction skipped" no-quit.py '    return a / (a + b)' '    return p' "dt: raw 0.6 is corrected to 0.444 and passes at moderate"
mutant "nested train mix not read" no-quit.py '        train = train[pack["winner"]]' '        pass' "dt: logs judge, raw p, adjusted p, threshold, strength"
mutant "threshold comparison flipped" no-quit.py '        return p["quit"] >= (p.get(' '        return p["quit"] < (p.get(' "dt strength moderate blocks raw 0.75"
# decision 2026-10-30 (b): a phrase hit lowers the local judge's bar
mutant "phrase hit ignored by the local judge" no-quit.py '"threshold_with_hit", p["threshold"]) if hits else' '"threshold_with_hit", p["threshold"]) if False else' "dt: raw 0.55 with an offer blocks at moderate"
mutant "with-hit bar applied without a hit" no-quit.py '"threshold_with_hit", p["threshold"]) if hits else' '"threshold_with_hit", p["threshold"]) if True else' "dt strength moderate passes raw 0.55"
mutant "with-hit bar not capped at the dial" no-quit.py '    return strength, thr, min(thr, prob(' '    return strength, thr, max(0.0, prob(' "dt: a with-hit bar above the dial is capped at the dial"
mutant "strength dial ignored" no-quit.py '    thr = prob(th[strength])' '    thr = prob(th["moderate"])' "dt strength hard blocks raw 0.55"
mutant "server down does not fall back to the rules" no-quit.py '        err("local", e)
        return None' '        err("local", e)
        return {"quit": 0.0, "raw": 0.0, "threshold": 0.5, "strength": "moderate"}' "server down: an offer still blocks by the phrase rule"
mutant "hook question sent instead of the pack's" no-quit.py '"question": pack["question"]' '"question": QUIT_Q' "dt: question is the pack's, verbatim"
mutant "reply head instead of its ending" no-quit.py 'reply[-1500:]' 'reply[:1500]' "dt: reply ending is the last 1,500 characters"
mutant "background flag dropped" no-quit.py '("yes" if bg else "no")' '"no"' "dt: a launch this turn, still running, is background work"
mutant "finished launches count as running" no-quit.py 'bg = bool(bg_launch) and (live_tasks(payload) or not isinstance(payload.get("background_tasks"), list))' 'bg = bool(bg_launch)' "dt: a launch that finished is not background work"
mutant "six-hour window dropped" no-quit.py 'time.time() - at <= BG_WINDOW_S' 'False' "dt: a launch within 6 hours before the request is background work"
mutant "dt probability logged as jev" no-quit.py '"jev": p if used == "jev" else None' '"jev": p' "dt: its probability is not logged as jev (only the cloud score may move quit_min)"

mutant "relative model_dir reads the cwd pack" no-quit.py '    if not os.path.isabs(md):  # ""' '    if False:  # ""' "model_dir '': no cwd pack.json is read or posted"
mutant "pack.json left out of the tamper check" no-quit.py '            bad.append("pack.json")' '            pass' "tamper: an edited pack.json is logged"
mutant "thresholds.json ignored" no-quit.py '    th = own if own and own is not DT_THRESHOLDS else mt.get("dt_thresholds") or DT_THRESHOLDS' '    th = own if own and own is not DT_THRESHOLDS else DT_THRESHOLDS' "dt: thresholds.json raises the bar, raw 0.75 passes"
mutant "thresholds.json with-hit bar ignored" no-quit.py 'mt.get("dt_thresholds_with_hit") or DT_THRESHOLDS_WITH_HIT' 'DT_THRESHOLDS_WITH_HIT' "dt: thresholds.json with-hit bar, an offer at raw 0.75 passes"
mutant "invalid thresholds.json falls back to the v3 bars" no-quit.py '        row = d[key]
        out[key] = {s: prob(row[s]) for s in' '        row = d.get(key, {})
        out[key] = {s: prob(row.get(s, 0.5)) for s in' "dt: an invalid thresholds.json steps the judge aside, the rules decide"
mutant "thresholds.json left out of the tamper check" no-quit.py '            bad.append("thresholds.json")' '            pass' "tamper: an edited thresholds.json is logged"
# decision 2026-10-30: short replies by the rules alone when the local judge is configured (short_reply_chars)
SP='    elif "local" in judge_order(cfg) and len(text.strip()) < short_cutoff(cfg):'
mutant "short path never taken" no-quit.py "$SP" '    elif False:' "short: probe blocks at moderate, no model call (0 tool calls): I will wire the CI in a later session."
mutant "short path for every reply" no-quit.py "$SP" '    elif "local" in judge_order(cfg):' "short: a 400-character reply goes to the model"
mutant "short path without the local judge" no-quit.py "$SP" '    elif len(text.strip()) < short_cutoff(cfg):' "short: rules-only config (no model_dir) has no short path"
mutant "cutoff counts untrimmed text" no-quit.py "$SP" '    elif "local" in judge_order(cfg) and len(text) < short_cutoff(cfg):' "short: a 399-character reply padded with blanks is short"
mutant "cutoff inclusive" no-quit.py "$SP" '    elif "local" in judge_order(cfg) and len(text.strip()) <= short_cutoff(cfg):' "short: a 400-character reply goes to the model"
mutant "short path still calls the judge" no-quit.py '        path, used, p = "short", "phrases", None' '        used, p = judge(cfg, user, claims, tstr, state); path = "short"' "short: probe blocks at moderate, no model call (0 tool calls): The fix is ready. Want me to push it?"
mutant "default cutoff off" no-quit.py 'SHORT_REPLY_CHARS = 400' 'SHORT_REPLY_CHARS = 0' "short: the default cutoff applies with no config key"
mutant "invalid cutoff silently off" no-quit.py '        err("short_reply_chars", "invalid_using_default")
        return SHORT_REPLY_CHARS' '        return 0' "short: an invalid short_reply_chars falls back to 400 and is logged"
mutant "needed your call is a handoff again" no-quit.py 'r"|(?<!need )(?<!needs )(?<!needed )(?<!requires )your call|' 'r"|your call|' "regression synthetic-1, rules: If the migration really needed your call, because the schema"
mutant "your call dropped" no-quit.py 'r"|(?<!need )(?<!needs )(?<!needed )(?<!requires )your call|' 'r"|' "your call: handing back a decided step still blocks"
# gate-safe nudge (2026-10-30): each reason must open with the gate rule
mutant "first nudge without the gate rule" no-quit.py 'FIRST_NUDGE = GATE_RULE + (' 'FIRST_NUDGE = (' "gate rule: first nudge"
mutant "gated nudge without the gate rule" no-quit.py 'GATED_NUDGE = GATE_RULE + (' 'GATED_NUDGE = (' "gate rule: gated-step nudge"
mutant "command nudge without the gate rule" no-quit.py 'CMD_NUDGE = GATE_RULE + (' 'CMD_NUDGE = (' "gate rule: unclassified-command nudge"
mutant "rotated nudges without the gate rule" no-quit.py 'ROTATE = tuple(GATE_RULE + x for x in (' 'ROTATE = tuple(x for x in (' "gate rule: first and rotated nudges (cap loop)"
mutant "scope nudge without the gate rule" no-quit.py 'SCOPE_NUDGE = GATE_RULE + (' 'SCOPE_NUDGE = (' "gate rule: every nudge constant, scope included"
mutant "gate rule clause emptied" no-quit.py '"around it: reply with one NEEDS-YOU: line naming the gated step (or BLOCKED: quoting the denial) and stop. "' '"around it. "' "gate rule: first nudge"
mutant "give-up beside a valid NEEDS-YOU voids it" no-quit.py 'n or tok, extra)' 'n, extra)' "gate-safe synthetic reply passes: harborline_deploy"
mutant "done sentence dropped" no-quit.py '"If everything the owner asked is done and verified, end with a short summary and stop; do not make unrequested "' '""' "done sentence: first, gated-step and command nudges"
mutant "announcement send not a gated step" no-quit.py '|announcement)s?\b"' ')s?\b"' "gate-safe synthetic reply passes: orchard_announce"
mutant "send to a list not a gated step" no-quit.py '(list|subscribers|addresses|recipients)\b"' '(xyzzylist)\b"' "gate-safe synthetic reply passes: tidewell_mail"
mutant "production-deploy word order not a gated step" no-quit.py '    r"|\b(prod|production) (deploy\w*|release|rollout|push|publish)\b"' '    r"|\bxyzzy\b"' "gate-safe synthetic reply passes: harborline_prod_bundle"
mutant "ship/release/promote to production not a gated step" no-quit.py '    r"|\b(ship(s|ping)?|releas(e|es|ing)|promot' '    r"|\bxyzzy\b|\b(xyzzy' "approval pattern: a lone consent ask passes: Want me to ship the bundle to production?"
mutant "deploy verbs reach a far target" no-quit.py ' ([\w.@/-]+ ){0,2}to (the )?(prod|production|live|release branch)' ' ([\w.@/-]+ ){0,3}(in)?to (the )?(prod|production|live|release branch)' "approval pattern: an ordinary offer still blocks: Want me to update the release notes to point to production?"
mutant "rotating a production key not a gated step" no-quit.py '|rotat|revok)' ')' "approval pattern: a lone consent ask passes: Should I rotate the production key?"
mutant "publishing a package not a gated step" no-quit.py '    r"|\bpublish\w* (the|this' '    r"|\bxyzzy (the|this' "approval pattern: a lone consent ask passes: Should I publish the package?"
# handoffs (owner report, 2026-10-30): a command or approval handed to the owner blocks without a matching named denial
mutant "handoff: handed() never fires" no-quit.py '        if bang or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):' '        if False:' "handoff: the same denial, not named, blocks"
mutant "handoff: a ! command alone is not handed" no-quit.py '        if bang or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):' '        if OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):' "handoff: a bare ! command block blocks"
mutant "handoff: run/paste asks dropped" no-quit.py '        if bang or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):' '        if bang or APPROVAL_HANDOFF.search(prose):' "handoff: run this in your terminal after tool calls blocks"
mutant "handoff: approval phrases dropped" no-quit.py '        if bang or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):' '        if bang or OWNER_RUN.search(prose):' "handoff regression, rules: 2_needs_your_approve_command_last_message"
mutant "handoff: \$ examples count as handed" no-quit.py '        bang = [c for c in codes if re.match(r"\s*! ", c)]' '        bang = [c for c in codes if re.match(r"\s*[!$] ", c)]' "handoff: a code block that is just an example passes"
mutant "handoff: whole reply, not the closing part" no-quit.py '    for s in code_sentences(text)[-5:]:' '    for s in code_sentences(text):' "handoff: an approval phrase early in a long report passes"
mutant "handoff: code split at ! (sentences())" no-quit.py '    for s in code_sentences(text)[-5:]:' '    for s in sentences(text)[-5:]:' "handoff: the nudge names the handed command"
mutant "handoff: a matching denial no longer excuses" no-quit.py '            gate = "denial"
        elif pre' '            gate = ""
        elif pre' "handoff: a handed command after a quoted classifier denial this turn passes"
mutant "handoff: any denial excuses any command" no-quit.py '        recs = [r for i, r in (denied or ()) if same_command(hcmd, i)]' '        recs = [r for i, r in (denied or ())]' "handoff: a denial of a different command does not excuse"
mutant "handoff: an unnamed denial excuses" no-quit.py 'if recs and len(recs) >= (2 if pre else 1) and denial_scoped(body, text, recs, n, extra):' 'if recs and len(recs) >= (2 if pre else 1):' "handoff: the same denial, not named, blocks"
mutant "handoff: a gated step loses its gate" no-quit.py '        elif pre or gate not in ("gated_consent", "owner_approval"):' '        else:' "handoff: a handed command for a gated step with NEEDS-YOU passes"
mutant "handoff: rule left to the judge" no-quit.py '    elif handoff:  # decided by the rule: no judge call' '    elif False:  # decided by the rule: no judge call' "handoff: decided by the rule, no judge call"
mutant "handoff nudge without the gate rule" no-quit.py 'HANDOFF_NUDGE = GATE_RULE + (' 'HANDOFF_NUDGE = (' "gate rule: handoff nudge"
mutant "handoff nudge not used" no-quit.py '    elif handoff and (pre or not step):' '    elif False:' "handoff: nudge says run it yourself, after the gate rule"
mutant "handoff nudge for a gated handed step" no-quit.py '    elif handoff and (pre or not step):' '    elif handoff:' "handoff: a gated handed command keeps the do-not-run nudge"
# standing approvals (~/.claude/realstop/standing.txt)
mutant "standing: file never read" no-quit.py 'pre = standing_hit(text, standing())' 'pre = standing_hit(text, [])' "standing: a pre-approved step asked as NEEDS-YOU blocks"
mutant "standing: does not override the consent gate" no-quit.py '        elif pre or gate not in ("gated_consent", "owner_approval"):' '        elif gate not in ("gated_consent", "owner_approval"):' "standing: a pre-approved step asked as NEEDS-YOU blocks"
mutant "standing: one denial excuses (no retry needed)" no-quit.py 'len(recs) >= (2 if pre else 1)' 'len(recs) >= 1' "standing: one named denial is not enough (no retry)"
mutant "standing: nudge does not say pre-approved" no-quit.py '| ({"the owner pre-approved it (standing approval: %s)" % pre[:80]} if pre else set()))' ')' "standing: the nudge says the owner pre-approved it"
mutant "standing: bad regex crashes the hook" no-quit.py '                    out.append((s, re.compile(re.escape(s), re.I)))' '                    raise' "standing: a bad regex line is matched as text and logged"
 
# ---- owner bypass, gentleness, duplicate registration (tests/safety.sh) ----
mutant "bypass: paused is ignored" no-quit.py 'if c.get("paused") is True:' 'if False:' "bypass: paused, an offer passes"
mutant "bypass: let it stop is never consumed" no-quit.py 'once.pop(sid, None)  # consumed by this stop, or expired' 'pass' "bypass: ...once only"
mutant "bypass: a subagent consumes let it stop" no-quit.py 'if fresh and sub:' 'if False:' "bypass: a subagent stop passes under 'once' but does not consume it"
mutant "bypass: let it stop never expires" no-quit.py 'fresh = time.time() - float(once[sid]) <= CONTROL_TTL_S' 'fresh = True' "bypass: an expired 'let it stop' is ignored"
mutant "gentle: cap stays configured" no-quit.py 'cap = min(cap, 1)' 'pass' "gentle: first week, one nudge per turn"
mutant "gentle: --strength does not end it" no-quit.py 'if not d.get("strength_set") and time.time()' 'if time.time()' "gentle: --strength ends it"
mutant "gentle: never ends" no-quit.py 'float(d["installed_at"]) + GENTLE_DAYS * 86400' 'float(d["installed_at"]) + 10 ** 12' "gentle: after 7 days the configured cap applies"
mutant "bypass: phrase matches inside a sentence" correction.py 'BYPASS_OFF = re.compile(r"^\s*realstop (off|pause)\s*[.!]?\s*$", re.I)' 'BYPASS_OFF = re.compile(r"realstop (off|pause)", re.I)' "bypass: a sentence containing the words does not pause"
mutant "dedupe: prompt lock not exclusive" correction.py 'os.O_CREAT | os.O_EXCL | os.O_WRONLY' 'os.O_CREAT | os.O_WRONLY' "dedupe: a second registration of the correction hook handles the same prompt once"
# ---- realstop learn (spec: mutants for the floor check, the cap and the confirmation gate; run them with MUTANT_FILTER=learn) ----
mutant "learn shift: floor dropped" no-quit.py 'return min(max(bar + shift, min(FLOOR, bar)), max(BAR_MAX, bar))' 'return bar + shift' "learn: bounds: shifted() stays within 0.10, the floor and 0.99"
mutant "learn shift: hook cap dropped" no-quit.py '    shift = max(-SHIFT_MAX, min(SHIFT_MAX, shift))
' '' "learn: hook: a -0.5 shift is clamped to -0.10 (bar 0.4066): corrected 0.38 passes"
mutant "learn shift: ceiling dropped" no-quit.py 'max(BAR_MAX, bar))' 'bar + 9)' "learn: bounds: shifted() stays within 0.10, the floor and 0.99"
mutant "learn shift: total cap dropped" scripts/realstop_learn.py 'round(max(-mx, min(mx, shift.get(s, 0.0) + (-STEP if q > wb else STEP))), 2)' 'round(shift.get(s, 0.0) + (-STEP if q > wb else STEP), 2)' "learn: the shift never exceeds 0.10 in total (40 missed quits)"
mutant "learn shift: no margin needed" scripts/realstop_learn.py 'if abs(q - wb) >= NEED:' 'if abs(q - wb) >= 1:' "learn: 3 more missed quits than wrong blocks: no shift yet"
mutant "learn shift: wrong blocks do not cancel" scripts/realstop_learn.py 'if abs(q - wb) >= NEED:' 'if q >= NEED or wb >= NEED:' "learn: a wrong block and a missed quit cancel: no shift from 4+4"
mutant "learn shift: broken file moves the bar" no-quit.py 'return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0' 'return float(v)' "learn: hook: a broken shift file moves nothing"
mutant "learn gate: no TTY needed" scripts/realstop_learn.py 'if not sys.stdin.isatty():' 'if False:' "learn: no TTY: refuses with exit 2"
mutant "learn gate: agent env ignored" scripts/realstop_learn.py '        if k in os.environ:' '        if False:' "learn: agent env CLAUDE_PID on a TTY: refuses, exit 2"
mutant "learn gate: undo and reset skip the check" scripts/realstop_learn.py 'readonly = bool(set(args) & {"--list", "--status", "--installer-note"})' 'readonly = True' "learn: --undo and --reset also refuse without a TTY"
mutant "learn gate: regression check off" scripts/realstop_learn.py '        if (pers["pairs_right"] < ship["pairs_right"] - GATE_DROP - 1e-9 or pers["accuracy"] < ship["accuracy"] - GATE_DROP - 1e-9
                or pers["false_blocks"] > ship["false_blocks"] + GATE_DROP + 1e-9):' '        if False:' "learn: refit refuses an all-mislabeled batch outright (pull ladder exhausted)"
mutant "learn gate: side check off" scripts/realstop_learn.py '        if not all(side):' '        if False:' "learn: contradictory examples are refused by the side check"
mutant "learn: undo leaves the head" scripts/realstop_learn.py '    note = rebuild(st)
    say("removed' '    note = ""
    say("removed' "learn: --undo removes it: no personal head, scores back to as-shipped exactly"
mutant "learn: reset keeps the head" scripts/realstop_learn.py '"shift.json", "head.safetensors", "regression.bin"' '"shift.json", "regression.bin"' "learn: --reset: scores are exactly as shipped"
mutant "learn: no pull toward the shipped head" scripts/realstop_learn.py 'lam = mu * sum(g[i][i] for i in range(k)) / k' 'lam = 1e-6 * sum(g[i][i] for i in range(k)) / k' "learn: refit with zero examples is the shipped head bit for bit; the pull shrinks the change; a head file round-trips"
mutant "learn: skip stays pending no more" scripts/realstop_learn.py '        if ans.startswith("s"):
            continue' '        if ans.startswith("s"):
            rv = reviewed(); rv["items"][it["key"]] = {"decision": "discarded"}; atomic(lp("reviewed.json"), json.dumps(rv).encode())
            continue' "learn: y stores one example; n discards; s keeps pending"
mutant "learn: states file world-readable" no-quit.py 'os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)' 'os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)' "learn: hook: states.jsonl is mode 0600"
mutant "learn: state masked before storing" no-quit.py 'json.dumps({"id": sid, "at": at, "session": session, "state": state})' 'json.dumps({"id": sid, "at": at, "session": session, "state": redact(state)})' "learn: hook: the stored state keeps text the log redactor would mask"
mutant "learn: rules-decided stops keep a state" no-quit.py 'if used == "local":  # its own key' 'if used in ("local", "phrases"):  # its own key' "learn: hook: a stop the rules decided keeps no state"
echo "mutants killed: $killed/$total"
exit $fail

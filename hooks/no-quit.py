#!/usr/bin/env python3
"""Stop hook: refuse to end a turn that gives up or hands decided work back, without proof of a real block.

Owner request: stop not doing what you CAN do and quitting. Cascade (owner decision 2026-10-30):
  1. deterministic rules and gates (live stop, plan mode, harness denials, sentence-scoped consent gates, a step
     handed to the owner, standing approvals in standing.txt): settled
     first, an excused stop is sent to no judge;
  2. the local DecisionTune v3 judge (one quit question, served on loopback): its yes
     probability is corrected to the real class mix (pack.json) and blocks at or above the strength dial's threshold,
     or at or above the lower with-hit threshold when the phrase rule also hits (owner decision 2026-10-30, option b);
  3. only when it has no answer and "cloud_ok" is true, the cloud judge (`jev`: quit, gate, done);
  4. when no judge answers (no server, no key, offline, timeout), the phrase rule (give-up, handoff, deferral,
     announcement, unearned caution; code and quotes stripped) decides alone.
Blocks again after a nudge only if a tool call followed it (held otherwise), at most block_cap (5) per user turn.
Evidence over claims (review 2026-10-30 #3, re-review): a stop is a real gate only when this turn's tool results hold a
harness denial (classifier, hook, owner rejection; never command output, which the agent can print itself) that the
reply names, or one sentence asks consent for a gated action (publish, deploy to prod, delete, spend, send) or for the
step the owner's request said needs approval, and nothing else in the reply gives up, offers or defers. A "BLOCKED:"
line or a quoted command error alone is a claim (logged, never excusing). Gates and shadow are decided before any judge.
Never overrides a live "stop" (anthropics/claude-code#77105), fails open (and logs the exception type to
errors.jsonl). Also runs on SubagentStop. The cloud judge (`jev`) only with "cloud_ok": true in config.json; local_url must be
loopback. learned.json (written only by `realstop learn`, from corrections the owner confirmed) may only move the cloud judge's quit_min within [0.35, 0.9] (lower after a missed quit, higher after a false block) and add examples. A manifest written by
install.sh detects edits to this file, config.json or the local pack.json; on a mismatch the hook enforces DEFAULTS.
Ideas from platcrest/checkpoint-guard, ferdousbhai/keep-going, /goal and Nanako0129/stingray. Python 3.9 compatible.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~/.claude/realstop")
LEARNED = os.path.join(HOME, "learned")  # `realstop learn`'s folder: states.jsonl (this hook writes), the rest is the CLI's
FLOOR = 0.35  # learned quit_min never goes below this (`realstop learn` has the same floor)
CEILING = 0.9  # nor above this, unless the owner's own quit_min is higher (`realstop learn` has the same ceiling)
JEV_TIMEOUT_S = 6
LOCAL_TIMEOUT_S = 3
BUDGET_S = 12  # every judge call together, well inside install.sh's 20 s Stop timeout (review #19)
DEADLINE = time.monotonic() + BUDGET_S  # one hook run per process
# strength dial for the local v3 judge: block when the real-mix-corrected p(yes) is at or above the threshold. Chosen on
# the v3-quit pack's 694 SELECT rows (453 negatives) for 5% / 10% / 15% false alarms; recall there 40% / 55% / 65%.
# Noisy: bootstrap 5-95% ranges 0.59-0.75, 0.38-0.57, 0.30-0.38. Re-derive with the model (2026-10-30, dial.py).
DT_THRESHOLDS = {"light": 0.6813, "moderate": 0.5066, "hard": 0.3267}
# with a phrase hit (offer, deferral, announcement, give-up words with zero tools, unearned caution) the bar drops to
# these (technical report, tuning of the with-hit bar): the lowest value per strength whose wrong nudges on
# the same 694 SELECT rows stay within 5% / 10% / 15% (reached 4.9% / 9.7% / 14.6%; recall 39.8 -> 44.8%, 53.1 -> 54.8%,
# 62.2 -> 64.7%). Short textbook quits score 0.14 to 0.26 and still pass at moderate; catching them needs about 0.13,
# which costs 15.2% wrong nudges on select ("probe-pinned" in the tuning record).
DT_THRESHOLDS_WITH_HIT = {"light": 0.5881, "moderate": 0.3363, "hard": 0.2264}
# owner decision 2026-10-30: with the local judge configured, a reply shorter than this (characters, reply.strip()) is
# decided by the rules alone and no judge is called; the model scores short textbook quits 0.14 to 0.26. Chosen on the
# v3-quit pack's 694 SELECT rows from {150, 200, 300, 400, 600}: the best recall with wrong nudges at or under 10% at
# moderate (technical report, short-reply rule). Ceiling: a length heuristic; a long reply that is all filler still goes to the model.
SHORT_REPLY_CHARS = 400
DEFAULTS = {"judge": "auto", "local_url": "http://127.0.0.1:8765", "model_dir": "", "strength": "moderate",
            "dt_thresholds": DT_THRESHOLDS, "dt_thresholds_with_hit": DT_THRESHOLDS_WITH_HIT, "quit_min": 0.6, "quit_with_hit": 0.3, "gate_max": 0.5, "examples": [],
            "phrases": [], "block_cap": 5, "shadow": False, "scope_check": False,
            "short_reply_chars": SHORT_REPLY_CHARS}
BG_WINDOW_S = 6 * 3600  # background work launched within this window (or since the owner's request) is relevant

GIVE_UP = (
    r"can'?t|cannot|can not|unable to|isn'?t possible|not possible|impossible|not built( yet)?|isn'?t built"
    r"|you'?ll need to|you need to (run|do)|you'?d need to|please run|run it yourself|do it yourself"
    r"|i'?ll leave|blocked on you|out of scope|beyond what i can|i couldn'?t|no way to"
)
HANDOFF = (
    r"want me to|do you want me|would you like( me)?|should i|shall i|let me know (if|how|whether|what)"
    # "your call" hands work back ("it's your call"); "needed your call" names a decision, often explaining a rubric
    # (tests/regressions/false_blocks.jsonl, 2026-10-30)
    r"|(?<!need )(?<!needs )(?<!needed )(?<!requires )your call|ready when you are|if you want,? i can|which (one )?(would you|do you) (like|prefer)"
    r"|say (the word|yes|go) and i|noted for later|left for later|(in|for) a later (pass|task|session)"
    r"|a follow-?up later|flag it if you want|i'?ll get to (it|that|this) later"
    # the agent's own work deferred behind an approval ("If you approve pytest, I'll make that edit", live miss
    # 2026-10-31); only rerunning the gated step itself ("I'll run it", "...and report the results") is no deferral
    r"|(if|once|when|after|as soon as) you('ve| have)? (approve|allow|ok|okay|confirm|grant|unblock|permit|enable|run(?! into| out))\w*"
    r"\b[^.;!?\n]{0,80}?,?\s+(then )?i('?ll| will| can| could|'?d| would) (?!(re-?)?(run|try|retry|execute)\b(\s[\w-]+){0,3}?"
    r"(\s(again|now|right away|for you))?(\sand (report|share|post|show|send)\w*( you| back)?(\s(the|its))?(\s\w+)?)?\s*([.!?\n]|$))\w+"
)
EVIDENCE = re.compile(
    r"(exit(ed)? (code )?\d|error[: ]|denied|classifier|permission|blocked by|refused|forbidden|\b40[134]\b"
    r"|EACCES|EPERM|traceback|failed with|rate limit|password|passkey|2fa|captcha"
    r"|irreversible|destructive|publish|spend|purchase|deploy to prod)",
    re.I,
)
# the reply NAMING a denial; it counts only when this turn's tool results hold a denial record (review #3)
DENIAL = re.compile(r"(classifier|hook|safety check) (denied|blocked)|denied by|permission (was )?denied"
                    r"|\[(CI Bypass|Data Exfiltration|Credential [A-Z]\w+)\]", re.I)
# a harness-authored denial, by how the record STARTS: a Bash failure always starts "Exit code N", so command output
# (printf 'denied by the classi%ser' fi) can never pose as one (re-review #2)
DENIAL_RECORD = re.compile(r"\s*(?:<tool_use_error>\s*)?(?:Permission for this action was denied by|PreToolUse:\w+ hook error:"
                           r"|The user doesn'?t want to proceed with this tool use|Blocked:)")
GATE_TOKEN = re.compile(r"^\s*(BLOCKED|NEEDS-YOU):", re.M)  # the model's explicit, machine-readable gate CLAIM
GATE_REASON = re.compile(r"^\s*(?:BLOCKED|NEEDS-YOU):[ \t]*(\S.{7,})$", re.M)  # a claim must carry a reason
# owner-only actions (review #1): the hook never pushes these; a reply that asks consent for one is a real gate.
# Qualified forms only: plain "push", "deploy", "delete", "send" stay ordinary work ("want me to push it?" is a quit).
GATED = re.compile(
    r"\b(npm|yarn|pnpm|poetry) publish\b|\btwine upload\b|\bgh release create\b|\bcargo publish\b|\bgem push\b"
    r"|\bdocker push\b|\bgit push\b[^\n]*(--force\b|\s-f\b|\b(prod|production|release|live)\b)"
    r"|\bdeploy\w*\b[^\n.]{0,40}\b(to|in|on) (the )?(prod|production|live)\b|\bterraform (apply|destroy)\b"
    # the other deploy verbs, verb forms only and "to" a close target: "promotion code works in production" is no deploy
    r"|\b(ship(s|ping)?|releas(e|es|ing)|promot(e|es|ing)|push(es|ing)?|roll(ing)?( it| this| that)? out) ([\w.@/-]+ ){0,2}to (the )?(prod|production|live|release branch)\b"
    r"|\bkubectl (apply|delete)\b(?![^\n]*\b(kind-|minikube|docker-desktop))|\brm -[a-zA-Z]*r[a-zA-Z]* +(/|~|\$HOME)"
    r"|\bsudo +\S|\b(purchase|buy|pay for|subscribe to) (the|a|an|this|that) \w+|\bcharge (the|my|your) card\b"
    r"|\bsend(ing)? (the|this|that|these|those|an?|my|our) ([\w.]+ ){0,2}(email|e-mail|message|dm|tweet|newsletter|invoice|announcement)s?\b"
    r"|\bsend(ing)? `[^`\n]+` to (the |all )?([\w-]+ ){0,2}(list|subscribers|addresses|recipients)\b"
    # same gated actions, other phrasings (GATE_SAFE_NUDGE 2026-10-30: "production deploy of bundle ..." was blocked)
    r"|\b(prod|production) (deploy\w*|release|rollout|push|publish)\b"
    r"|\b(releas|publish|ship|roll(ing)? out|launch)\w*\b[^\n.]{0,30}\bto (all |the )?(users|customers|public|subscribers)\b"
    r"|\b(post|announc|e-?mail|tweet)\w* (the|this|that|these|those|an?|my|our) ([\w.]+ ){0,2}(announcement|email|e-mail|message|tweet|newsletter|invoice|release|update|notes|post)s?\b"
    r"|\b(e-?mail|announc)\w* (the |all )?(list|subscribers|customers|users)\b"
    r"|\b(delete|drop|wipe|purge)\w* (the|all|every) ([\w-]+ ){0,2}(database|table|schema|bucket|volume|backups?|repo|repository)s?\b"
    r"|\b(delete|drop|wipe|purge|rotat|revok)\w* (the |all )?(production|prod|live)\b"
    r"|\bpublish\w* (the|this|that|an?|my|our) ([\w.@/-]+ ){0,2}(package|crate|gem|wheel|image|extension|plugin|release|version)s?\b"
    r"|\bpay(ing)? (the|this|that|an?|my|our) ([\w.]+ ){0,2}(invoice|bill|fee|fine|subscription|license|balance)\b"
    r"|\bcharge (the|a|an) (customer|client|account)s?\b|\bcharg(e|ing) \$\d|\b(make|submit|authorize|issue) (a |the |this )?(payment|purchase|refund)\b", re.I)
GATED_SQL = re.compile(r"\b(DROP|TRUNCATE) (TABLE|DATABASE|SCHEMA)\b|\bDELETE FROM\b")  # case-sensitive: SQL, not prose
# destructive or outward steps the hook does not excuse but must never push either (re-review #4): they only
# switch the nudge to GATED_NUDGE. Ceiling: a pattern list; an unlisted command gets the neutral CMD_NUDGE instead.
RISKY = re.compile(
    r"\brm\s+-[a-zA-Z]*[rf]|\b(drop|truncate)\s+(table|database|schema)\b|\bdelete\s+from\b|\breset\s+--hard\b"
    r"|\bclean\s+-[a-zA-Z]*f|--prod\b|--force\b|\bs3\s+(rb|rm)\b|\b(aws|gcloud|az)\b[^\n`]{0,60}\b(delete|destroy|terminate)"
    r"|\brefunds?\b|\bstripe\s+\w+\s+(create|delete|update)\b", re.I)
# the action an owner's approval clause names ("ask me before deleting") and a consent sentence must name too
ACTION = re.compile(r"\b(delet|remov|publish|push|deploy|releas|merg|send|e-?mail|post|drop|truncat|purchas|buy|pay|spend"
                    r"|charg|migrat|renam|overwrit|reset|ship|submit|upload|rm)(?:e|es|ed|ing|s|ion|ions|ment|ments|ping|ped)?\b", re.I)
CONSENT = re.compile(
    r"\b(want me to|do you want me|would you like|should i|shall i|ok(ay)? to|go ahead|approv\w*|consent|confirm"
    r"|your (go|ok|sign-?off)|needs? (your|the owner'?s)|with your permission)\b", re.I)
OWNER_APPROVAL = re.compile(
    r"\b(needs?|requires?) my (approval|ok|okay|consent|sign-?off|go-?ahead)|\bask (me )?(first|before)\b|\bcheck with me\b"
    r"|\bwait for my (ok|okay|go|approval)\b|\b(don'?t|do not|never) \w+(\s\w+){0,3} without (me|my|asking|approval)\b", re.I)
# handing the owner a step the agent can run itself (owner report, 2026-10-30: "if you ask me to paste or run
# anything it's only because you already tried to run it and failed!"). Not consent gates unless the step is a real
# gated one (GATED, an owner approval clause): real_gate settles those first. Ceiling: wording lists.
APPROVAL_HANDOFF = re.compile(
    r"\b(waits?|waiting|sits?|sitting|held|blocked|parked) (on|for) (your|the owner'?s) (approv\w*|ok(ay)?|go-?ahead"
    r"|sign-?off|command|confirmation)\b|\b(awaiting|pending) your (approv\w*|ok(ay)?|go-?ahead|sign-?off|command)\b"
    r"|\b(needs?|waits? (on|for)|waiting (on|for)|run|paste) your (\w+ )?command\b|\byour approve command\b"
    r"|\bneeds? your (approval|go-?ahead|sign-?off|ok(ay)?)\b"
    r"|\bthe (command|one|line|step)s? (in|from) my (last|previous|earlier) (message|reply|turn)\b"
    r"|\bonce you (approve|run|paste|confirm|sign off)\b", re.I)
OWNER_RUN = re.compile(
    # "you'll need to run" stays a give-up word (ignored after a tool call, the measured trade-off), not a handoff
    r"\b(please|can you|could you|just|for you to) (run|paste|type|execute|approve)\b"
    r"|\b(paste|run|execute|type) (this|these|it|that|the following|the (command|line)s?)( (in|into) your \w+)?\s*:?\s*$"
    r"|\bpaste (this|these|it|that|the (command|line)s?)\b", re.I)
ANNOUNCE = re.compile(
    r"\b(i'?ll now|i will now|next,? i('?ll| will)|now i('?ll| will)|i'?m going to|i am going to|let me now)\b", re.I)
# unearned caution (owner feedback, 2026-10-29): a proposed wait, fallback or retention must name what
# it protects and the evidence; folded in from ~/.claude/hooks/no-unearned-caution.py. Ceiling: phrases; other wordings slip.
CAUTION = re.compile(
    r"\b(for (a|one|two|\d+) (more )?(day|week|month)s?|rollback window|as a (fallback|safety net)|to be safe"
    r"|just in case|until (it'?s|everything is|things are) stable|keep (it|them|this|that|both) (around|for now)"
    r"|leave (it|them) in place for now|wait (a|one) (day|week)|after a soak|soak (period|for)"
    # a review period proposed for the owner ("after you've seen a day of its log", 2026-10-31 miss)
    r"|after (you'?ve|you have) (seen|reviewed|looked at) (a|one|two|\d+) (day|week)s?|(a|one|two|\d+) (day|week)s? of (its |the )?logs?"
    r"|(stay|stays|leave it|keep it) in report-only)\b", re.I)
PAST_SPAN = re.compile(r"\b((has|have|had)|\w+'s) been\b|\b(was|were)\b|\b(sat|ran|stayed|lasted|took|spent)\b(\s+\w+)?\s*$", re.I)
WAKE_PLAN = re.compile(r"\b(wakes? (me|up)|check-?ins?|heartbeat|notif(y|ies|ication)|monitor)\b", re.I)
CAUTION_EVIDENCE = re.compile(
    r"(\b[\w./-]+\.(ts|js|py|sh|json|ya?ml|swift|md):\d+|\bexit(ed)? (code )?\d|\bmeasured\b|\bI (ran|checked|verified|tested)\b"
    r"|\bfailed (with|on)\b|\b(the owner|the user|you) (asked|said|decided)\b)", re.I)
SCOPE_TOOLS = ("Edit", "Write", "NotebookEdit")
INJECTED = ("<system-reminder>", "<local-command", "<command-name>", "<command-message>", "<hook_prompt",
            "<task-notification>", "[Request interrupted", "Stop hook feedback", "Caveat:")
HARNESS_KEYS = ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")  # user records the harness wrote
# a live stop is an imperative at the start of a sentence or clause with no object ("stop", "please stop now", "wait,
# why", "stop and wait"), anywhere in the message. "Do not stop until done", "don't touch the schema without asking",
# "wait for CI", "fix the stop button" are not stops (review #16, re-review #6).
USER_STOP = re.compile(
    r"(?:^|(?<=[.!?;,\n]))\s*(?:(?:no|ok|okay|hey|please|just|now|so|but|and|then)[,.!]?\s+)*"
    r"(?:stop|pause|halt|abort|cancel|hold (?:on|off|up)|wait(?: for (?:me|my \w+))?|leave it(?: alone| be| there)?"
    r"|don'?t(?: do)?(?: it| that| this)?)"
    r"(?:\s+(?:it|now|here|there|that|this|everything|all|working|please|immediately|right now|for now"
    r"|for a (?:sec|second|moment|minute)))*\s*(?:$|[.!?,;:\n\u2014\u2013-]|and\b)", re.I | re.M)


ERR_MAX_BYTES, ERR_KEEP = 256 * 1024, 1000


def err(where: str, e) -> None:
    """Every swallowed exception leaves a line (type only, never reply text): a silently removed guard is visible."""
    try:
        os.makedirs(HOME, exist_ok=True)
        path = os.path.join(HOME, "errors.jsonl")
        try:
            if os.path.getsize(path) > ERR_MAX_BYTES:  # cap: keep the newest ERR_KEEP lines (a stopped server logs one per stop)
                with open(path, encoding="utf-8") as f:
                    keep = f.readlines()[-ERR_KEEP:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": int(time.time()), "hook": "no-quit", "where": where,
                                "type": e if isinstance(e, str) else type(e).__name__}) + "\n")
    except OSError:
        pass


def _json(name: str):
    try:
        with open(os.path.join(HOME, name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        err(name, e)
        return {}


def sha256(path: str):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def tampered() -> list:
    """Files that differ from install.sh's manifest (this hook, config.json, the local pack.json). Detection only: an agent running as the owner can rewrite the manifest too (review #13)."""
    try:
        with open(os.path.join(HOME, "manifest.json"), encoding="utf-8") as f:
            files = json.load(f)["files"]
        bad = []
        if sha256(os.path.realpath(__file__)) != files.get("no-quit.py"):
            bad.append("no-quit.py")
        if sha256(os.path.join(HOME, "config.json")) != files.get("config.json"):
            bad.append("config.json")
        # the local pack's question and class mixes steer every local decision (review 7880f89 #3); config.json,
        # checked above, names it. Ceiling: the weights (1.5 GB) are not hashed per stop; the pinned file list records them.
        md = str(_json("config.json").get("model_dir") or "")
        if (sha256(os.path.join(md, "pack.json")) if os.path.isabs(md) else None) != files.get("pack.json"):
            bad.append("pack.json")
        # the model's own thresholds.json steers the block decision just as much (absent = None on both sides)
        if (sha256(os.path.join(md, "thresholds.json")) if os.path.isabs(md) else None) != files.get("thresholds.json"):
            bad.append("thresholds.json")
        return bad
    except FileNotFoundError:
        # the installed copy must have its manifest: deleting it is tamper, not "no check" (re-review #5).
        # Repo and test copies run outside ~/.claude/realstop and are not checked.
        return ["manifest.json"] if os.path.dirname(os.path.realpath(__file__)) == os.path.realpath(HOME) else []
    except Exception as e:
        err("manifest", e)
        return ["manifest.json"]


def learned(tamper: bool = False) -> dict:
    """Defaults, then config.json (the owner's choices), then learned.json, which may only move quit_min (down after
    a missed quit, up after a false block) within [FLOOR, CEILING] (widened to the owner's own quit_min if outside) and add examples: never judge, shadow, local_url, cloud_ok or block_cap (review #13).
    Tampered: DEFAULTS only (no shadow, no cloud, default cap and thresholds)."""
    cfg = dict(DEFAULTS)
    if tamper:
        return cfg
    cfg.update(_json("config.json"))
    lj = _json("learned.json")
    q = lj.get("quit_min")
    if isinstance(q, (int, float)) and math.isfinite(q):
        cfg["quit_min"] = max(min(FLOOR, cfg["quit_min"]), min(max(CEILING, cfg["quit_min"]), q))
    ex = lj.get("examples")
    if isinstance(ex, list):
        cfg["examples"] = list(cfg.get("examples") or []) + [x for x in ex if isinstance(x, str)]
    return cfg


def strip_quoted(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"^\s*>.*$", " ", text, flags=re.M)
    return re.sub(r"[\"“][^\"”\n]{0,300}[\"”]", " ", text)


def head_tail(s: str, head: int, tail: int) -> str:
    """A long reply keeps its opening (where a blocker is usually named) and its ending."""
    return s if len(s) <= head + tail else s[:head] + " [...] " + s[-tail:]


def redact(s: str) -> str:
    """Scrubs the local stops.jsonl tail only (a log hygiene step, not part of any judge path)."""
    s = re.sub(r"(?i)\b(password|passwd|passphrase|pwd|token|secret|api[_-]?key)\b(\s*[:=]\s*|\s+is\s+)[^\s;,]+",
               r"\1: [redacted]", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", s)
    s = re.sub(r"/(Users|home)/[^/\s]+", "~", s)
    s = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[redacted]", s)
    return re.sub(r"\+?\d[\d\s().-]{8,}\d", "[number]", s)


def owner_text(part: str) -> str:
    """The owner's words in one user text part: whole <system-reminder> blocks dropped; a part that STARTS with a harness
    marker (reminder, command echo, hook feedback, Caveat:) is harness text. Position, never substring: an owner asking
    about "<system-reminder>" tags is still the owner (re-review #6)."""
    s = re.sub(r"<system-reminder>.*?</system-reminder>", " ", part, flags=re.S).strip()
    return "" if s.startswith(INJECTED) else s


def command_turn(body: str) -> str:
    """A slash command or skill the owner typed with args ("/ship build it") is a real turn: its name plus args.
    Echoes without args (/compact) and local-command output stay harness text."""
    m = re.search(r"<command-name>\s*(.*?)\s*</command-name>", body, re.S)
    a = re.search(r"<command-args>\s*(.*?)\s*</command-args>", body, re.S)
    if m and a and a.group(1) and "<local-command" not in body and "<system-reminder>" not in body:
        return m.group(1) + "\n" + a.group(1)
    return ""


def confine(path: str) -> str:
    """A transcript path outside ~/.claude/projects is treated as unreadable (keep-going 387-399)."""
    try:
        root = os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects"))
        real = os.path.realpath(path) if path else ""
        return real if real.startswith(root + os.sep) else ""
    except Exception:
        return ""


def result_text(c: dict) -> str:
    v = c.get("content")
    if isinstance(v, list):
        return "\n".join(str(x.get("text", "")) for x in v if isinstance(x, dict))
    return v if isinstance(v, str) else ""


def stamp(d: dict):
    """A transcript record's ISO timestamp as epoch seconds, None when absent or unparseable."""
    try:
        return datetime.datetime.fromisoformat(str(d["timestamp"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def from_transcript(path: str) -> tuple:
    """(last real user text, count of real user messages, assistant text since it, tool names called since it,
    this turn's error tool results as text, whether relevant background work was launched, this turn's harness denials
    as (the denied call's command or input, the denial text)). tools is None when the
    file cannot be read. Records the harness marks (isMeta, isCompactSummary, isVisibleInTranscriptOnly) are never the
    owner's request. A launch (run_in_background, Monitor) is relevant when it came after the owner's request or
    within BG_WINDOW_S; crons never count. Ceiling: launches are not matched to the payload's tasks by id."""
    user, n, texts, tools, errors, bg_turn, bg_recent, denied, calls = "", 0, [], [], [], False, False, [], {}
    if not path:
        return user, n, "", None, [], False, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if d.get("type") == "user" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") is True:
                            errors.append(result_text(c)[:4000])
                            if DENIAL_RECORD.match(errors[-1]):
                                denied.append((calls.get(str(c.get("tool_use_id")), ""), errors[-1]))
                if d.get("type") == "user" and not any(d.get(k) for k in HARNESS_KEYS) and not d.get("toolUseResult"):
                    parts = [content] if isinstance(content, str) else [
                        str(c.get("text", "")) for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
                    body = " ".join(command_turn(x) or owner_text(x) for x in parts)
                    if body.strip():
                        user, n, texts, tools, errors, bg_turn, denied = body, n + 1, [], [], [], False, []
                elif d.get("type") == "assistant" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            texts.append(str(c.get("text", "")))
                        elif isinstance(c, dict) and c.get("type") == "tool_use":
                            tools.append(str(c.get("name", "")))
                            inp = c.get("input")
                            calls[str(c.get("id"))] = str(inp.get("command") or json.dumps(inp)) if isinstance(inp, dict) else ""
                            if (isinstance(inp, dict) and inp.get("run_in_background") is True) or c.get("name") == "Monitor":
                                bg_turn = True
                                at = stamp(d)
                                bg_recent = bg_recent or (at is not None and time.time() - at <= BG_WINDOW_S)
    except OSError:
        return "", 0, "", None, [], False, []
    return user, n, "\n".join(texts), tools, errors, bg_turn or bg_recent, denied


def left() -> float:
    return DEADLINE - time.monotonic()


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if key:
        return key
    try:  # a 0600 file written by install.sh (for a machine with no secrets store)
        with open(os.path.join(HOME, "typesafe.key"), encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    try:
        r = subprocess.run(["/bin/bash", "-lc", "secrets get TYPESAFE_API_KEY"], capture_output=True, text=True,
                           timeout=max(0.1, min(5, left())))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


QUIT_Q = (
    "Does `reply` end the assistant's turn while work remains that the assistant could do itself right now? Yes if "
    "it gives up or says something cannot be done without showing an attempt, offers or asks permission for an "
    "obvious next step, defers work for later, or announces an action without saying it was done.")
GATE_Q = (
    "Does `reply` name a concrete blocker that only the owner can clear: consent for an irreversible, destructive, "
    "outward-facing or spending action, a missing password or credential, or an exact error or denial message the "
    "assistant hit? Also yes if work the assistant started is still running in the background and will report back "
    "when it finishes.")
DONE_Q = "Does `reply` report that the work asked for in `request` is finished, with nothing it promised left undone?"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a 3xx becomes an HTTPError: a local judge never forwards the text elsewhere


LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)  # no http_proxy, no redirects


def loopback(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        return u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "::1", "localhost")
    except Exception:
        return False


def prob(v) -> float:
    v = float(v)
    if not (math.isfinite(v) and 0.0 <= v <= 1.0):
        raise ValueError("probability out of range")
    return v


def dt_state(request: str, reply: str, n: int, bg: bool) -> str:
    """The v3 training format, exactly: the request's first 600 characters, the raw reply's last 1,500 (code and quotes
    kept, as in training), this turn's tool-call count, and whether relevant background work is running."""
    return ("request: " + request[:600] + "\nreply ending: " + reply[-1500:] + "\ntool calls this turn: " + str(n)
            + "\nbackground work running: " + ("yes" if bg else "no"))


def correct(p: float, pack: dict) -> float:
    """p(yes) from a pack trained on a balanced mix, moved to the real mix: yes odds times
    (real yes / real no) / (train yes / train no), 0.532 for v3-quit. Applied before any threshold."""
    real, train = pack["real_mix"], pack["train_mix"]
    if isinstance(train.get(pack.get("winner")), dict):  # dtpack nests the mix under the winning kind ("deep")
        train = train[pack["winner"]]
    ry, rn, ty, tn = (prob(x) for x in (real["yes"], real["no"], train["yes"], train["no"]))
    if not (ry and rn and ty and tn):
        raise ValueError("class mix has a zero")
    a, b = p * ry / ty, (1 - p) * rn / tn
    return a / (a + b)


def model_thresholds(md: str) -> dict:
    """The strength dials that belong to the model in md: md/thresholds.json, {"dt_thresholds": {light, moderate, hard},
    "dt_thresholds_with_hit": {same}} (a model's score scale is its own: v3s-quit's moderate is about 0.83, v3's 0.5066).
    {} when the file is absent, so the built-in v3 constants apply; ValueError when it is present but not valid, so the
    judge steps aside (rules decide) instead of running a model on another model's bars."""
    try:
        with open(os.path.join(md, "thresholds.json"), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    out = {}
    for key in ("dt_thresholds", "dt_thresholds_with_hit"):
        row = d[key]
        out[key] = {s: prob(row[s]) for s in ("light", "moderate", "hard")}
    return out


def strength_bars(cfg: dict, md: str) -> tuple:
    """(strength, block bar, with-hit bar) of the local judge as shipped, before any learned shift. The owner's
    config.json wins; else the model's own thresholds.json; else the built-in v3 constants. A cfg value that is still the
    DEFAULTS object was not set by the owner. Also read by `realstop learn` (scripts/realstop_learn.py)."""
    mt = model_thresholds(md)
    own = cfg.get("dt_thresholds")
    th = own if own and own is not DT_THRESHOLDS else mt.get("dt_thresholds") or DT_THRESHOLDS
    ownw = cfg.get("dt_thresholds_with_hit")
    thw_row = ownw if ownw and ownw is not DT_THRESHOLDS_WITH_HIT else mt.get("dt_thresholds_with_hit") or DT_THRESHOLDS_WITH_HIT
    strength = cfg.get("strength")
    if strength not in th:
        err("strength", "unknown_using_moderate")
        strength = "moderate"
    thr = prob(th[strength])
    return strength, thr, min(thr, prob(thw_row.get(strength, thr)))


SHIFT_MAX = 0.10  # `realstop learn` moves a bar by at most this much (steps of 0.02, after 4 net confirmations)
BAR_MAX = 0.99


def shifted(bar: float, shift: float) -> float:
    """bar plus the learned shift, kept within SHIFT_MAX of the shipped bar and inside [FLOOR, BAR_MAX] (a shipped bar
    already below FLOOR, like hard's with-hit 0.2264, may be raised but not lowered further)."""
    shift = max(-SHIFT_MAX, min(SHIFT_MAX, shift))
    return min(max(bar + shift, min(FLOOR, bar)), max(BAR_MAX, bar))


def learned_shift(strength: str) -> float:
    """learned/shift.json, written only by `realstop learn`: {"light": x, "moderate": x, "hard": x}. 0.0 when absent
    or not a finite number (a broken file must not move a bar)."""
    try:
        with open(os.path.join(LEARNED, "shift.json"), encoding="utf-8") as f:
            v = json.load(f)[strength]
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def local_judge(cfg: dict, state: str) -> dict | None:
    """The quit question of the v3 pack (read from its pack.json, verbatim) to a local DecisionTune server
    (scripts/realstop-serve.sh). Loopback only, no proxy, no redirects: nothing leaves the Mac. None when the server
    is down or slow, the pack is unreadable or the URL is not loopback: the next judge in line decides.
    The server also returns p_shipped and p_personal (the shipped head and the personal head `realstop learn` refit,
    same forward pass); "probabilities" is the one in use. Both are corrected to the real mix and logged."""
    url = cfg["local_url"]
    if not loopback(url):
        err("local_url", "refused_not_loopback")
        return None
    md = str(cfg.get("model_dir") or "")
    if not os.path.isabs(md):  # "" or relative would read pack.json from the hook's cwd, the project (review 7880f89 #2)
        err("local", "model_dir_not_absolute")
        return None
    try:
        with open(os.path.join(md, "pack.json"), encoding="utf-8") as f:
            pack = json.load(f)
        strength, thr0, thw0 = strength_bars(cfg, md)
        shift = learned_shift(strength)
        thr = shifted(thr0, shift)
        thw = shifted(thw0, shift)  # strength_bars already caps thw0 at the dial, and shifted() keeps the order
        req = urllib.request.Request(
            url.rstrip("/") + "/decide", method="POST", headers={"Content-Type": "application/json"},
            # options in the order a dtpack yes/no pack trained with ([no, yes]); serve's default order is
            # [yes, no], which moves a trained pack's p_yes by up to 0.18 (measured 2026-10-29)
            data=json.dumps({"state": state, "question": pack["question"], "options": ["no", "yes"]}).encode())
        with LOCAL_OPENER.open(req, timeout=min(LOCAL_TIMEOUT_S, left())) as r:
            body = json.load(r)
        raw = prob(body["probabilities"]["yes"])
        both = {}
        for k in ("p_shipped", "p_personal"):  # absent from an older server, null without a personal head
            try:
                both[k] = correct(prob(body[k]), pack) if body.get(k) is not None else None
            except (KeyError, ValueError, TypeError):
                both[k] = None
        return {"quit": correct(raw, pack), "raw": raw, "threshold": thr, "threshold_with_hit": thw,
                "strength": strength, "shift": shift, "shipped": both["p_shipped"], "personal": both["p_personal"]}
    except Exception as e:
        err("local", e)
        return None


def judge(cfg: dict, request: str, reply: str, tools: str = "", state: str = "") -> tuple:
    """(judge used, probabilities or None). cfg["judge"]: phrases | jev | local | auto (local, then jev, then phrases).
    The first judge that answers decides; the next runs only when it has no answer. state is the local judge's input
    (dt_state); empty when the turn could not be read, and then the local judge is skipped.
    Pick it with install.sh --judge or by editing ~/.claude/realstop/config.json. The cloud judge (jev) runs only with
    "cloud_ok": true in config.json (install.sh --judge jev writes it); never inferred from a key (review #2)."""
    for name in judge_order(cfg):
        if left() < 1:  # one deadline across every judge: a killed hook logs nothing, a skipped judge does
            err("deadline", "skipped_" + name)
            break
        if name == "local" and not state:
            err("local", "no_state")
            continue
        p = local_judge(cfg, state) if name == "local" else jev(request, reply, cfg["examples"], tools)
        if p is not None:
            return name, p
    return "phrases", None


def judge_order(cfg: dict) -> list:
    """The judges to ask, in order (see judge())."""
    choice = os.environ.get("NO_QUIT_JUDGE") or cfg["judge"]
    if os.environ.get("NO_QUIT_JEV") == "off" and choice in ("jev", "auto"):
        choice = "phrases" if choice == "jev" else "local"
    # auto puts the local model first only once a trained pack is configured (model_dir, install.sh --judge local):
    # the base model is near noise on this question (2026-10-29: 0.15 to 0.64 on one reply by wording).
    auto = ["local", "jev"] if cfg.get("model_dir") else ["jev"]
    order = {"phrases": [], "jev": ["jev"], "local": ["local"], "auto": auto}.get(choice, auto)
    if cfg.get("cloud_ok") is not True:
        order = [x for x in order if x != "jev"]
    return order


def short_cutoff(cfg: dict) -> float:
    """cfg["short_reply_chars"], or the default when it is not a finite number >= 0 (logged, never silently 0)."""
    v = cfg.get("short_reply_chars", SHORT_REPLY_CHARS)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        err("short_reply_chars", "invalid_using_default")
        return SHORT_REPLY_CHARS
    return v


def jev(request: str, reply: str, examples: list, tools: str = "") -> dict | None:
    """Three Noul probabilities, or None when the cloud judge cannot answer: the phrase rule then decides.
    The state is sent as built (opt-in cloud judge; no redaction or PII check)."""
    key = api_key()
    if not key:
        return None
    seen = ""
    if examples:
        seen = " Replies like these were rejected by the owner as quitting: " + " | ".join(str(x) for x in examples[-8:])
    body = {
        "model": "jev-latest",
        "state": {"request": request[-1500:], "reply": head_tail(reply, 1000, 2000)},
        "questions": {
            "quit": {"type": "noul", "instructions": QUIT_Q + seen},
            "gate": {"type": "noul", "instructions": GATE_Q},
            "done": {"type": "noul", "instructions": DONE_Q},
        },
    }
    if tools:
        body["state"]["tools"] = tools  # evidence the reply alone lacks (stingray turn-tools.jq); the local judge's format stays fixed
    data = json.dumps(body)
    if left() < 1:
        err("deadline", "skipped_jev")
        return None
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone", data=data.encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=min(JEV_TIMEOUT_S, left())) as r:
            a = json.load(r)["answers"]
        return {q: prob(a[q]["noul"]) for q in ("quit", "gate", "done")}
    except Exception as e:
        err("jev", e)
        return None


def keep_state(session: str, state: str) -> str:
    """The judge state exactly as the local model saw it (no masking: masking changes the vector), for `realstop learn`
    to embed if the owner later corrects this stop. learned/states.jsonl, mode 0600, local only; pruned after 30 days
    unless the owner confirmed it (learn.py, `realstop learn`). Never read by the cloud (jev) example path. "" on failure."""
    try:
        at = int(time.time())
        sid = hashlib.sha1(("%s|%d|%s" % (session, at, state)).encode()).hexdigest()[:12]
        os.makedirs(LEARNED, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(LEARNED, "states.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": sid, "at": at, "session": session, "state": state}) + "\n")
        return sid
    except OSError as e:
        err("states", e)
        return ""


def log(entry: dict) -> None:
    """Probabilities, phrase hits and a short redacted tail of the reply (local only): learn.py's input."""
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "stops.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": int(time.time()), **entry}) + "\n")
    except OSError:
        pass


def decide(hits: list, p: dict | None, cfg: dict) -> bool:
    """Real gates are settled before this (real_gate); the reply's own "error"/"denied" words excuse nothing."""
    if p is None:
        return bool(hits)  # no model judge: the phrase rule alone
    if "threshold" in p:  # the local v3 judge: corrected p(yes) against the strength dial, lower with a phrase hit
        return p["quit"] >= (p.get("threshold_with_hit", p["threshold"]) if hits else p["threshold"])
    # A real gate always lets the stop through; the cloud judge's quit can block a give-up the phrases miss,
    # and must agree (a lower bar) before a phrase hit blocks.
    return p["gate"] < cfg["gate_max"] and (
        p["quit"] >= cfg["quit_min"] or (bool(hits) and p["quit"] >= cfg["quit_with_hit"]))


def armed(payload: dict) -> bool:
    """Work this session started is still running and will wake it: an unfinished background task or a session cron.
    A missing or non-list field is unknown, never "nothing running" (keep-going 778-783, stingray 316-345)."""
    crons = payload.get("session_crons")
    return live_tasks(payload) or (isinstance(crons, list) and len(crons) > 0)


def live_tasks(payload: dict) -> bool:
    tasks = payload.get("background_tasks")
    return isinstance(tasks, list) and any(
        not isinstance(t, dict) or str(t.get("status", "running")).lower() not in ("completed", "failed", "done", "killed")
        for t in tasks)


def denial_named(body: str, text: str, errors: list) -> bool:
    """A harness denial record (classifier, hook, owner rejection) is in this turn's tool results and the reply names
    it: its bracket label, or denial wording of the same kind outside code and quotes. Excuses nothing alone: see
    denial_scoped (a denial of one action no longer excuses unrelated unfinished work, live miss 2026-10-31)."""
    recs = [e for e in errors if DENIAL_RECORD.match(e)]
    if not recs:
        return False
    low = text.lower()
    if any(lab.lower() in low for raw in recs for lab in re.findall(r"\[([A-Z][\w ]{2,40})\]", raw)):
        return True
    m = DENIAL.search(body)
    if not m:
        return False
    cats = [c for c in ("classifier", "hook", "safety check") if c in m.group(0).lower()]
    return not cats or any(c in raw.lower() for c in cats for raw in recs)


def sentences(text: str) -> list:
    """The reply's sentences and lines; a fenced block is folded into the line that introduces it."""
    t = re.sub(r"\n?```[^\n]*\n(.*?)```", lambda m: " `" + " ".join(m.group(1).split()).replace("`", "'") + "`", text, flags=re.S)
    return [x for x in re.split(r"(?<=[.!?])\s+|\n+", t) if x.strip()]


def token_line(s: str) -> bool:
    return bool(re.match(r"\s*(BLOCKED|NEEDS-YOU):[ \t]*\S", s))


def asks(s: str) -> bool:
    return bool(CONSENT.search(s) or token_line(s))


def stems(s: str) -> set:
    return {m.group(1).lower() for m in ACTION.finditer(s)}


def owner_actions(user: str) -> set:
    """Action stems in the owner's approval clauses this turn: "ask me before deleting any files" -> {"delet"}."""
    u, out = strip_quoted(user), set()
    for m in OWNER_APPROVAL.finditer(u):
        a = max(u.rfind(c, 0, m.start()) for c in ".;!?,\n") + 1
        ends = [i for i in (u.find(c, m.end()) for c in ".;!?,\n") if i >= 0]
        out |= stems(u[a:min(ends) if ends else len(u)])
    return out


def gated_step(text: str) -> str:
    m = GATED.search(text) or GATED_SQL.search(text)
    return m.group(0) if m else ""


# "not built" as product-roadmap status in an explanatory answer is not a give-up (tests/regressions/false_blocks.jsonl,
# 2026-10-30): the same sentence marks it as roadmap ("planned, not built", "on the roadmap"), or it sits in a sentence
# or bullet list framed as claims the product does not make. "That's not built, so I'll stop here" has neither and blocks.
NOT_BUILT = re.compile(r"\b(?:is|are|was|were)?\s*(?:not|isn'?t|aren'?t) built(?: yet)?\b", re.I)
ROADMAP = re.compile(r"\bplanned\b|\bon (?:the|our|a) roadmap\b|\broadmap item\b", re.I)
NOT_CLAIMED = re.compile(r"\b(?:not|isn'?t|aren'?t) claiming\b|\b(?:we|i|it) (?:don'?t|do not|doesn'?t|does not) claim\b"
                         r"|\bnot (?:claiming|promising)\b", re.I)
BULLET = re.compile(r"\s*(?:[-*\u2022]|\d+[.)])\s")


def roadmap_status(s: str) -> str:
    """s with 'not built' removed where it is roadmap status or sits under a not-claiming frame (a pattern list; an
    unlisted framing still counts as a give-up word)."""
    out, framed = [], False
    for line in s.split("\n"):
        if BULLET.match(line):
            keep = framed
        else:
            keep = False
            if line.strip():
                framed = bool(NOT_CLAIMED.search(line))  # a non-bullet line resets the frame, or opens one
        parts = re.split(r"(?<=[.!?])\s+", line)
        out.append(" ".join(NOT_BUILT.sub(" ", x) if keep or ROADMAP.search(x) or NOT_CLAIMED.search(x) else x for x in parts))
    return "\n".join(out)


def quit_hits(s: str, n, extra: list) -> set:
    """Give-up, handoff, deferral, announcement and unearned-caution hits in s (code and quotes already stripped)."""
    s = roadmap_status(s)
    # give-up words alone mostly mark a real gate reported after an attempt ("you need to enter your password"):
    # they count only when this turn tried nothing, or the turn is unknown (owner feedback, 2026-10-29; blind rows: wrong
    # blocks 69 -> 32 of 363, precision 37% -> 51%)
    parts = [HANDOFF] + extra + ([GIVE_UP] if not n else [])
    hits = {m.group(0).lower() for m in re.finditer(r"\b(" + "|".join(parts) + r")\b", s, re.I)}
    lines = s.strip().splitlines()
    while lines and re.fullmatch(r"\s*(#{1,6}\s.*|[-*_]{3,}|\*\*[^*]+\*\*:?)?\s*", lines[-1]):
        lines.pop()  # a trailing heading or rule does not push the announcement out of the last sentences
    # the reply is the turn's last word: an action it announces was not done, whatever tools ran before it
    if ANNOUNCE.search(" ".join(re.split(r"(?<=[.!?])\s+|\n+", "\n".join(lines).strip())[-3:])):
        hits.add("announced, not done")
    # evidence must back the caution itself: it counts in the same sentence or the next one, never anywhere in the
    # reply ("I ran the tests" earlier excused a later "keep it a week to be safe", 2026-10-31 miss)
    ss = sentences(s)
    for i, t in enumerate(ss):
        # a wake plan ("check in every 30 minutes as a fallback") schedules my own follow-up; it proposes no wait for the owner
        if WAKE_PLAN.search(t) or CAUTION_EVIDENCE.search(" ".join(ss[i:i + 2])):
            continue
        # "has been running for 4 days" reports a past duration; only a proposed one ("keep it for a week") is caution
        hits.update("unearned caution: " + m.group(0).lower() for m in CAUTION.finditer(t)
                    if not (m.group(0).lower().startswith("for ") and PAST_SPAN.search(t[:m.start()])))
    return hits


def excused(sents: list, pred, n, extra: list) -> bool:
    """Some sentence asks consent for the gated or owner-approved step itself (pred), and nothing else in the reply
    gives up, offers, defers or announces: a gate excuses only its own sentence (re-review #1, #3)."""
    own = [x for x in sents if asks(x) and pred(x)]
    # a valid NEEDS-YOU:/BLOCKED: line naming the step is the reply's last word: a give-up word elsewhere ("can't run
    # without the code") explains the gate and does not void it; offers, deferrals and announcements still do
    tok = any(token_line(x) for x in own)
    return bool(own) and not quit_hits(strip_quoted("\n".join(x for x in sents if x not in own)), n or tok, extra)


def denial_scoped(body: str, text: str, recs: list, n, extra: list) -> bool:
    """The reply names a harness denial in recs, and the sentences that do not name it give up, offer, defer or announce
    nothing: a denial excuses only the denied step, as excused() scopes a consent gate. Give-up words count only when
    this turn tried nothing (a denial record means it tried). Ceiling: a sentence that names the denial is never checked,
    so an offer in that same sentence slips (sentence split, as in excused())."""
    if not denial_named(body, text, recs):
        return False
    ss = sentences(text)
    rest = [x for x in ss if not denial_named(strip_quoted(x), x, recs)]
    return not quit_hits(strip_quoted("\n".join(rest)), n or any(token_line(x) for x in ss), extra)


def real_gate(user: str, text: str, body: str, errors: list, n, extra: list) -> str:
    """Why this stop is a real gate, from evidence, never from the reply's own claim; "" when it is not one.
    A quoted command error is never evidence: the agent can print any error it likes (re-review #2)."""
    if errors and denial_scoped(body, text, errors, n, extra):
        return "denial"
    sents, acts = sentences(text), owner_actions(user)
    if acts and excused(sents, lambda x: bool(acts & stems(x)), n, extra):
        return "owner_approval"
    if excused(sents, lambda x: bool(gated_step(x)), n, extra):
        return "gated_consent"
    return ""


def asked_command(text: str) -> str:
    """The code a consent-asking reply names (inline or fenced), for the neutral nudge; "" when none."""
    if not any(asks(x) for x in sentences(text)):
        return ""
    for m in re.finditer(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", text, re.S):
        c = " ".join((m.group(1) or m.group(2) or "").split())
        # an inline span is a command only as "program args": a lone name (`tabs`) or a file path is no command
        # (live 2026-10-31). Ceiling: an absolute-path program (`/usr/bin/git push`) falls to the generic nudge.
        if m.group(1) or (" " in c and re.match(r"(\./)?[\w.+-]+$", c.split()[0])):
            return c[:80]
    return ""


def standing() -> list:
    """The owner's standing approvals, ~/.claude/realstop/standing.txt: one phrase or regex per line, "#" comments.
    Not in the tamper manifest (owner-edited at will, no install.sh rerun): an entry only makes the hook stricter, a
    deleted entry loosens it back to the default. A line that is not a valid regex is matched as plain text (logged)."""
    out = []
    try:
        with open(os.path.join(HOME, "standing.txt"), encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                try:
                    out.append((s, re.compile(s, re.I)))
                except re.error:
                    err("standing", "bad_regex_as_text")
                    out.append((s, re.compile(re.escape(s), re.I)))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        err("standing", e)
    return out


def code_sentences(text: str) -> list:
    """sentences(), but code is never split: a fenced block's "! cd ..." or an inline `a. b` stays one piece, folded
    into the line that introduces it."""
    codes = []
    def keep(m):
        codes.append(" ".join((m.group(1) if m.group(1) is not None else m.group(2)).split()).replace("`", "'"))
        return " \x00%d\x00" % (len(codes) - 1)
    t = re.sub(r"\n?```[^\n]*\n(.*?)```|`([^`\n]+)`", keep, text, flags=re.S)
    return [re.sub(r"\x00(\d+)\x00", lambda m: "`" + codes[int(m.group(1))] + "`", x)
            for x in re.split(r"(?<=[.!?])\s+|\n+", t) if x.strip()]


def handed(text: str) -> tuple:
    """(True, command) when the reply's closing part (its last 5 sentences; a fenced block folds into the line that
    introduces it) hands the owner a command or asks the owner to run, paste or approve a step: a "! " command (Claude
    Code's run-it-in-your-shell prefix), a run/paste ask (OWNER_RUN), or an approval handoff (APPROVAL_HANDOFF). A "$ "
    block or any code with no such ask is an example, not a handoff. command is the handed code, "" when none."""
    for s in code_sentences(text)[-5:]:
        codes = re.findall(r"`([^`\n]+)`", s)
        bang = [c for c in codes if re.match(r"\s*! ", c)]
        prose = strip_quoted(s)
        if bang or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose):
            return True, (bang or codes or [""])[0].strip()
    return False, ""


def same_command(cmd: str, denied_input: str) -> bool:
    """A denied call ran the handed command: its last segment verbatim, or its key verb (the segment's last word).
    No command (a phrase-only handoff): any denial counts. Ceiling: the verb match is loose ("approve" matches any
    denied approve)."""
    seg = re.split(r"&&|\|\||;|\|", re.sub(r"^\s*[!$]\s+", "", cmd))[-1].strip()
    if not seg:
        return True
    inp = " ".join(denied_input.split())
    words = re.findall(r"[A-Za-z][\w-]{2,}", seg)
    return " ".join(seg.split()) in inp or bool(words and re.search(r"\b" + re.escape(words[-1]) + r"\b", inp, re.I))


# A sentence that really asks the owner for consent. Narrower than CONSENT on purpose: the bare nouns and verbs there
# ("approvals", "consent", "confirm") also occur in text that only DESCRIBES the owner's settings, which must never
# match a standing entry (live false block 2026-10-30: a status report listing the owner's standing approvals).
STANDING_ASK = re.compile(
    r"\b(want me to|do you want me|would you like|should i|shall i|(can|may|could) i|ok(ay)? to|go ahead|with your permission"
    r"|your (go|ok|sign-?off)|needs? (your|the owner'?s)|(need|needs|requires?|awaiting|pending|asking for) (your |the owner'?s )?"
    r"(approval|consent|confirmation|go-?ahead|sign-?off)|(please|can you|could you|will you|would you) (approve|confirm|ok)\b)"
    r"|\?\s*$", re.I)


def standing_hit(text: str, pats: list) -> str:
    """The standing approval that a consent ask or handoff sentence names (code included), "" when none. Only a
    sentence addressed to the owner as an ask counts (STANDING_ASK on its unquoted prose, a NEEDS-YOU/BLOCKED line, a
    handoff or a `! ` command); descriptive text, a list of settings, quoted text or a bare code span never does."""
    for s in code_sentences(text):
        prose = strip_quoted(s)
        if (STANDING_ASK.search(prose) or token_line(s) or OWNER_RUN.search(prose) or APPROVAL_HANDOFF.search(prose)
                or re.search(r"`\s*! ", s)):
            for raw, rx in pats:
                if rx.search(s):
                    return raw
    return ""


QSET = hashlib.sha1("\n".join([QUIT_Q, GATE_Q, DONE_Q, GIVE_UP, HANDOFF, CAUTION.pattern, GATED.pattern, RISKY.pattern, APPROVAL_HANDOFF.pattern, OWNER_RUN.pattern]).encode()).hexdigest()[:12]  # criteria drift voids calibrated thresholds
# Safety (realstopbench REAL_NUDGE, 2026-10-30): a bare "keep going" nudge at a correct gate stop made Opus 5.5 attempt the
# gated step in 3 of 4 cases. EVERY reason therefore opens with the gate rule, before any "finish the work" guidance.
GATE_RULE = (
    "If a real gate stops you (owner approval or consent, a credential or login only the owner has, a production deploy, "
    "a send, post or publish, a payment, a delete, or a hook or classifier denial), do not attempt that step or work "
    "around it: reply with one NEEDS-YOU: line naming the gated step (or BLOCKED: quoting the denial) and stop. "
    # realstopbench GATE_SAFE_NUDGE: without this, 6 of 6 done stops got unrequested edits after the nudge (2 of 6 before)
    "If everything the owner asked is done and verified, end with a short summary and stop; do not make unrequested "
    "changes or offer more work. "
)
FIRST_NUDGE = GATE_RULE + (
    "Otherwise your reply gives up or hands decided work back ({why}) without showing a real block. "
    "The owner's standing order: do what you CAN do, be resilient, never quit. If nothing real blocks the "
    "rest (a destructive or outward-facing step that needs consent, a missing credential, an exact "
    "error or denial), do it now and report the result. If one gate is real, do every reversible step "
    "first, then name that one gate and the exact error or consent it needs. Do not re-ask or defer. "
    "A proposed wait, fallback or retention must name what it protects and the evidence (a check you ran, a file "
    "and line, a measured result); if it protects nothing, drop it and do the work now. "
    "To stop on a real gate, end with one line: BLOCKED: quoting the classifier, hook or owner rejection you hit "
    "this turn, or NEEDS-YOU: naming the one gated step (publish, deploy to production, delete, spend, send) that "
    "needs consent, with nothing else offered or deferred. A BLOCKED: line that quotes a command's own error does "
    "not end the turn: fix the error or try another route."
)
# review #1: when the reply mentions an owner-only step, the nudge must never read as "run it"
GATED_NUDGE = GATE_RULE + (
    "Your reply stops with work left ({why}); a step needs the owner's consent ({step}). "
    "Do not run that step. Finish every reversible step; ask consent only for the gated step, on one line starting "
    "NEEDS-YOU: that names it exactly."
)
# re-review #4: a command the hook cannot classify gets no execute-now imperative either
CMD_NUDGE = GATE_RULE + (
    "Your reply stops with work left ({why}) and asks about a command this hook cannot classify ({step}). "
    "Finish every reversible step of the request yourself and report the results. If that command deletes, deploys, "
    "publishes, sends or spends, do not run it: ask consent for it alone on one line starting NEEDS-YOU:."
)
# later nudges are short and fixed: a repeated paragraph reads as a stuck loop, a model-written reason invites argument
ROTATE = tuple(GATE_RULE + x for x in (
    "Still not done: take the next concrete step with a tool call now. If one step truly needs consent, finish the rest and name only it on a NEEDS-YOU: line.",
    "That stop was premature again. Do the remaining work yourself and show it with a tool call; only a real gate (NEEDS-YOU: or BLOCKED:) ends the turn.",
    "Keep going: the work you described is yours to finish. Run the next reversible step now; only a denial or a consent-gated step (NEEDS-YOU:) ends the turn.",
))
# Owner report, 2026-10-30: a command or approval handed to the owner that the agent can run itself
HANDOFF_NUDGE = GATE_RULE + (
    "Run it yourself now. If the owner has a standing approval for this kind of step, quote it when you retry. Hand it "
    "over only if your own retry is denied this turn, and quote the denial. Your reply hands the owner a step ({why})."
)
SCOPE_NUDGE = GATE_RULE + (
    "Before you stop: was the WHOLE request done, or a convenient slice of it? Re-read the request, check each part "
    "against what you actually changed and verified, and finish anything left. If it was all done, say so in one line."
)


GENTLE_DAYS = 7  # a new install nudges at most once per user turn for this long (or until install.sh --strength)
CONTROL_TTL_S = 3600  # a "let it stop" that nobody consumed expires


def control_read() -> dict:
    try:
        with open(os.path.join(HOME, "control.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def bypass(sub: bool, sid: str) -> str:
    """The owner's instant bypass (written by correction.py on "realstop off" / "let it stop", or install.sh --pause):
    "paused" lets every stop through until resumed; "once" lets this session's next stop through, then is consumed.
    A subagent stop never consumes a "once". Fails open: any error means no bypass, and the hook goes on."""
    try:
        c = control_read()
        if c.get("paused") is True:
            return "paused"
        once = c.get("once")
        if not (isinstance(once, dict) and sid and sid in once):
            return ""
        fresh = time.time() - float(once[sid]) <= CONTROL_TTL_S
        if fresh and sub:
            return "once"
        once.pop(sid, None)  # consumed by this stop, or expired
        tmp = os.path.join(HOME, "control.json.tmp%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, os.path.join(HOME, "control.json"))
        return "once" if fresh else ""
    except Exception as e:
        err("bypass", e)
        return ""


def effective_cap(cfg: dict) -> int:
    """block_cap, except one nudge per user turn during the first GENTLE_DAYS after a fresh install (install.json,
    written by install.sh; absent on older installs, which keep their configured cap) or until --strength was run."""
    cap = int(cfg["block_cap"])
    try:
        with open(os.path.join(HOME, "install.json"), encoding="utf-8") as f:
            d = json.load(f)
        if not d.get("strength_set") and time.time() < float(d["installed_at"]) + GENTLE_DAYS * 86400:
            cap = min(cap, 1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return cap


def state_path(key: str) -> str:
    return os.path.join(HOME, "state", re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80] + ".json")


def load_state(key: str) -> dict:
    try:
        with open(state_path(key), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(key: str, st: dict) -> None:
    # Ceiling: one small file per session, never pruned. Upgrade path: delete files older than 30 days in learn.py.
    try:
        os.makedirs(os.path.join(HOME, "state"), exist_ok=True)
        with open(state_path(key) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(state_path(key) + ".tmp", state_path(key))
    except OSError as e:
        err("save_state", e)


def duplicate(key: str, h: str) -> bool:
    """Atomic dedupe: the first of two parallel hook runs creates state/<key>.<hash>.lock (O_EXCL); a lock under
    30 s old means the other run already handled this stop. Stale locks are replaced. Fails open (not a duplicate)."""
    try:
        d = os.path.join(HOME, "state")
        os.makedirs(d, exist_ok=True)
        path = state_path(key)[:-5] + "." + h + ".lock"
        for f in os.listdir(d):  # ceiling: one scan per stop; fine for a few hundred files
            if f.endswith(".lock") and time.time() - os.path.getmtime(os.path.join(d, f)) > 60:
                os.unlink(os.path.join(d, f))
        for _ in range(2):
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return False
            except FileExistsError:
                if time.time() - os.path.getmtime(path) < 30:
                    return True
                os.unlink(path)
        return False
    except Exception as e:
        err("duplicate", e)
        return False


def evaluate(payload: dict, sub: bool, st: dict, stateful: bool, user: str, text: str, tools, errors=(),
             bg_launch: bool = False, denied=()) -> dict | None:
    """The block decision for one stop; st (per-session state) is updated in place. None lets the stop through."""
    n = len(tools) if tools is not None else None
    errors = list(errors or [])
    sid = payload.get("session_id", "")
    if payload.get("stop_hook_active"):
        if not stateful or n <= st["at_tools"]:
            log({"session": sid, "judge": "none", "hits": [], "block": False, "held": True, "tools": n, "qset": QSET})
            return None  # one nudge, or nudged and no tool call since: the agent weighed it and holds
    if not sub and USER_STOP.search(strip_quoted(user)):  # a subagent's "user" is its brief, not the owner
        log({"session": sid, "judge": "none", "hits": [], "block": False, "live_stop": True, "tools": n, "qset": QSET})
        return None  # never override a live "stop"
    bad = tampered()
    if bad:
        err("tamper:" + ",".join(bad), "tamper")
    cfg = learned(tamper=bool(bad))
    body = strip_quoted(text)
    # a reply that is all quote or code is judged whole: stripping must not hide the whole reply (review #14)
    claims = body if len(re.sub(r"\W", "", body)) >= 12 else text
    extra = [re.escape(x) for x in cfg["phrases"] if x]
    hits = quit_hits(claims, n, extra)
    blocks = re.findall(r"```([^\n`]*)\n(.*?)```", text, re.S)
    if blocks and text.rstrip().endswith("```") and blocks[-1][0].strip().lower() in ("", "text", "txt", "md", "markdown"):
        hits.update(m.group(0).lower() for m in re.finditer(r"\b(" + HANDOFF + r")\b", blocks[-1][1], re.I))  # an offer in a final prose block
    hits = sorted(hits)
    evidence = bool(EVIDENCE.search(claims))  # logged only: the reply's own words excuse nothing (review #3)
    gate = real_gate(user, text, body, errors, n, extra)  # before any judge call: an excused stop is never sent anywhere
    # a step handed to the owner, or a consent ask for a step the owner pre-approved (standing.txt), is a quit unless
    # this turn's harness denied that same command (twice with a standing approval: the try and the retry that quotes
    # it) and the reply names the denial. A real gated step keeps its gate unless the owner pre-approved it.
    hand, hcmd = handed(text)
    pre = standing_hit(text, standing()) if (hand or GATE_TOKEN.search(body) or any(asks(x) for x in sentences(text))) else ""
    if hand or pre:
        recs = [r for i, r in (denied or ()) if same_command(hcmd, i)]
        if recs and len(recs) >= (2 if pre else 1) and denial_scoped(body, text, recs, n, extra):
            gate = "denial"
        elif pre or gate not in ("gated_consent", "owner_approval"):
            gate = ""
    handoff = bool((hand or pre) and not gate)
    # a named denial that the rest of the reply voided (an offer, deferral or announcement beside it): decided by the
    # rule like a handoff (the local model scored the live miss 0.33, under every bar), and the nudge names the denied
    # command as the step not to run, so it never reads as "retry it" (realstopbench GATE_SAFE_NUDGE)
    named = [(i, r) for i, r in (denied or ()) if denial_named(body, text, [r])] if not gate else []
    dvoid = bool(named) and not denial_scoped(body, text, [r for _, r in named], n, extra)
    dstep = (" ".join(named[0][0].split())[:80] or "the step the harness denied") if dvoid else ""
    if handoff:
        hits = sorted(set(hits) | {"handed the owner a step" + (": " + hcmd[:80] if hcmd else "")}
                      | ({"the owner pre-approved it (standing approval: %s)" % pre[:80]} if pre else set()))
    claim = ""
    if GATE_TOKEN.search(body) and not gate:
        claim = "BLOCKED" if GATE_REASON.search(body) else "BLOCKED (no reason)"
    acts = owner_actions(user)
    risky = RISKY.search(text)
    step = dstep or gated_step(text) or (risky.group(0) if risky else "") or (
        "/".join(sorted(acts)) + ", which the owner asked to approve" if acts else "")
    cmd = "" if step else asked_command(text)
    shadow = bool(cfg["shadow"]) or (os.environ.get("REALSTOP_SHADOW") or os.environ.get("RESILIENT_SHADOW")) == "1"  # RESILIENT_* is the pre-rename name, read for one release
    if shadow:
        cfg["cloud_ok"] = False  # shadow mode makes no outbound call (review #2); a loopback judge may still score
    tstr = "%d tool calls: %s" % (n, ", ".join(sorted(set(tools))[:12])) if tools is not None else ""
    if tstr and errors:
        tstr += "; %d failed" % len(errors)  # command errors only inform the judge, never excuse (re-review #2)
    # relevant launch still running (payload tasks when given, the launch alone otherwise); crons never count
    bg = bool(bg_launch) and (live_tasks(payload) or not isinstance(payload.get("background_tasks"), list))
    state = dt_state(user, text, n, bg) if n is not None else ""
    # path: gate (excused before any judge), short (local judge configured, short reply: the rules alone, no judge
    # call), judge (some judge answered) or rules (no judge answered, or nothing left to judge)
    if gate:
        path, used, p = "gate", "none", None
    elif handoff:  # decided by the rule: no judge call
        path, used, p = "handoff", "none", None
    elif dvoid:  # decided by the rule: no judge call
        path, used, p = "denial_scope", "none", None
    elif not claims.strip():
        path, used, p = "rules", "phrases", None
    elif "local" in judge_order(cfg) and len(text.strip()) < short_cutoff(cfg):
        path, used, p = "short", "phrases", None
    else:
        used, p = judge(cfg, user, claims, tstr, state)
        path = "judge" if p is not None else "rules"
    kind = "quit" if not gate and (handoff or dvoid or decide(hits, p, cfg)) else ""
    # work it armed is still running and will wake the session (cliffhanger, keep-going): waiting is a real stop,
    # unless the reply also gives up or offers decided work in words. A subagent's stop ends its run: never excused.
    if kind and (not hits or hits == ["announced, not done"]) and not sub and armed(payload):
        kind = ""
    if (not kind and not gate and stateful and cfg["scope_check"] and p and "done" in p and p["done"] >= 0.8 and p["gate"] < cfg["gate_max"]
            and not st["scoped"] and any(t in SCOPE_TOOLS for t in tools)):
        kind = "scope"  # once per user turn
    if kind and stateful and st["turn_blocks"] >= effective_cap(cfg):  # per user turn, not per run of nudges
        kind = ""
    would = kind if shadow else ""
    if shadow:
        kind = ""
    entry = {"session": payload.get("session_id", ""), "judge": used, "hits": hits, "evidence": evidence,
             "jev": p if used == "jev" else None, "block": bool(kind), "tail": redact(body.strip())[-240:], "qset": QSET,
             "tools": n, "path": path}
    if used == "local":  # its own key: only the cloud score ("jev") can lower its quit_min
        entry["dt"] = {"raw": round(p["raw"], 4), "adjusted": round(p["quit"], 4), "threshold": p["threshold"],
                       "threshold_with_hit": p.get("threshold_with_hit"), "hit_applied": bool(hits), "strength": p["strength"],
                       # both heads from the same forward pass, so every learned influence is visible (None: no personal head)
                       "shipped": None if p.get("shipped") is None else round(p["shipped"], 4),
                       "personal": None if p.get("personal") is None else round(p["personal"], 4),
                       "shift": p.get("shift", 0.0)}
        entry["state_id"] = keep_state(payload.get("session_id", ""), state)
    if gate:
        entry["gate"] = gate
    if claim:
        entry["claim"] = claim  # a gate token with no evidence: logged, never excusing
    if step:
        entry["gated_step"] = True
    if handoff:
        entry["handoff"] = "standing" if pre else True
    if bad:
        entry["tamper"] = bad
    if shadow:
        entry.update(shadow=True, would_block=bool(would))
    if kind == "scope":
        entry["scope"] = True
    log(entry)
    if not kind:
        return None
    first = st.get("blocks", 0) == 0 if stateful else True
    if stateful:
        st["blocks"] += 1
        st["turn_blocks"] += 1
        st["at_tools"] = n
        st["scoped"] = st["scoped"] or kind == "scope"
    if kind == "scope":
        reason = SCOPE_NUDGE
    elif handoff and (pre or not step):  # a gated or risky step handed over keeps the do-not-run nudge
        reason = HANDOFF_NUDGE.format(why="; ".join(hits))
    elif step:
        reason = GATED_NUDGE.format(why=", ".join(hits) or "judged a premature stop", step=step)
    elif cmd:
        reason = CMD_NUDGE.format(why=", ".join(hits) or "judged a premature stop", step=cmd)
    elif first:
        reason = FIRST_NUDGE.format(why=", ".join(hits) or "judged a premature stop")
    else:
        reason = ROTATE[(st["blocks"] - 2) % len(ROTATE)]
    return {"decision": "block", "reason": reason}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        sub = payload.get("hook_event_name") == "SubagentStop"
        if (sub and not payload.get("agent_type")) or payload.get("permission_mode") == "plan":
            return  # internal agents (suggestions, /btw) and plan mode are not work to push
        why = bypass(sub, str(payload.get("session_id") or ""))
        if why:
            log({"session": str(payload.get("session_id") or ""), "judge": "none", "hits": [], "block": False, "bypass": why})
            return  # the owner said "realstop off" / "let it stop" (or ran install.sh --pause)
        user, nturn, text, tools, errors, bg_launch, denied = from_transcript(confine(payload.get("agent_transcript_path" if sub else "transcript_path") or ""))
        text = payload.get("last_assistant_message") or text
        sid = str(payload.get("session_id") or "")
        # parallel subagents of one type share a session id: key on agent_id, else on their own transcript path
        tpath = confine(payload.get("agent_transcript_path") or "")
        sub_id = str(payload.get("agent_id") or "") or (hashlib.sha1(tpath.encode()).hexdigest()[:12] if tpath else "")
        key = sid + ("." + sub_id if sub else "") if sid and (not sub or sub_id) else ""
        stateful = bool(key) and tools is not None
        st = load_state(key) if stateful else {}
        if stateful:
            if duplicate(key, hashlib.sha1((text + "|%d" % len(tools)).encode()).hexdigest()[:12]):
                return  # a second install of this hook, same reply and tool count, at the same moment
            tk = hashlib.sha1(user.encode()).hexdigest()[:8] + ":" + str(nturn)
            if st.get("turn") != tk:
                st.update(turn=tk, blocks=0, turn_blocks=0, at_tools=0, scoped=False)
            st.setdefault("blocks", 0)
            st.setdefault("turn_blocks", 0)
            st.setdefault("at_tools", 0)
            st.setdefault("scoped", False)
        out = evaluate(payload, sub, st, stateful, user, text, tools, errors, bg_launch, denied)
        if stateful:
            if out is None:
                st["blocks"] = 0  # an allowed stop ends the run of nudges
            save_state(key, st)
        if out:
            print(json.dumps(out))
    except Exception as e:  # fail open: a broken guard must never trap a turn, but leave a trace (review #19)
        err("main", e)
        return


if __name__ == "__main__":
    main()

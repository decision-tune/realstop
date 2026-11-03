# Changelog

## 0.2.2 (2026-11-03)

Package metadata only: add project links (homepage, repository, model, changelog, issues), keywords and classifiers for PyPI, and the real-session demo GIF and cropped banner in the README. No code changes.

## 0.2.1 (2026-11-02)

Fix the banner, replay GIF and repository links in the PyPI description.

## 0.2.0 (2026-11-02)

- Add a rules-only Codex CLI main-agent adapter with shadow reporting, opt-in bounded continuation, and independent setup, pause, resume, status and uninstall.

## 0.1.1 (2026-11-01)

- Ignore example questions inside quotes, Markdown tables and code when deciding whether a reply hands work back; questions outside examples still count.
- Use the SPDX Apache-2.0 license expression in package metadata.
- Clarify that runtime indicators supplement the learning review TTY check and are not a security boundary; preserve existing runtime detection.
- Remove references to an unpublished report from two implementation comments.

## 0.1.0 (research preview)

First public release, as a research preview.

- A Stop hook, a correction hook, a SessionStart housekeeping hook and the skill, installed by `realstop setup`.
- The `realstop` command (Python 3.9+, standard library only; `uv tool install .` or `pipx install .`): `setup`, `judge local|jev|rules`, `strength light|moderate|hard`, `learn`, `pause`, `resume`, `status`, `diagnose`, `serve [--keep]`, `uninstall`. `install.sh` installs the package, runs `realstop setup` and maps its flags to these commands.
- Judges: a local DecisionTune model (the default, with three strengths) with the phrase rules as automatic backup, and an opt-in cloud decision model.
- Approval-first block message: every nudge opens with the approval rule and never tells the agent to take a gated step.
- The model is fetched, not shipped: `scripts/fetch_model.py` checks the sha256 and size of every file and falls back to the rules on any failure (see MODEL.md). The file hashes and the Hugging Face revision are pinned.
- `realstop learn`: review-gated learning from your corrections. Nothing is learned automatically. A confirmed correction refits a personal copy of the local model's 4 KB head, accepted only if it passes a check on the released synthetic pairs (`data/regression_pairs.jsonl`, CC BY 4.0), and can move the block threshold in 0.02 steps (at most 0.10). For the cloud decision model it adds the example and moves `quit_min`. It needs a terminal and refuses when agent environment indicators are present.
- Instant bypass: type `realstop off` / `realstop on` or `let it stop` in a prompt. A fresh install nudges at most once per user turn for its first 7 days.
- `settings.json` edits are atomic and locked, keep every unrelated key and hook, back up before a change, refuse an invalid file, and avoid double registration with the Claude Code plugin.
- Claude Code plugin packaging: `.claude-plugin/plugin.json`, a one-plugin marketplace (`decision-tune`), `hooks/hooks.json` and `skills/realstop/`.
- Tests: `tests/run.sh` (offline, temp HOME, runs under the stock macOS `/bin/bash` 3.2), `tests/safety.sh`, and `scripts/leak_check.py`. Every fixture is synthetic.

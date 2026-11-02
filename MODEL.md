# The RealStop model: how it is fetched

RealStop's default judge is a DecisionTune pack (a ModernBERT-based encoder plus a one-unit head, fine-tuned for one yes/no question about a coding agent's final reply). The pack is not in this git repo. It is a separate artifact on Hugging Face. Its folder includes `thresholds.json`, the strength dials for this model's own score scale (moderate about 0.83); the hook reads it from the model folder and the install manifest hashes it.

**Status: pinned.** The model is v3s-quit, on Hugging Face at [decision-tune/realstop](https://huggingface.co/decision-tune/realstop). `scripts/model-files.txt` pins its 8 files by sha256 and size, and `MODEL_REVISION` pins the Hugging Face commit `6d40b7840b24671e456c822c5268e6137ed70c42`, so `realstop setup` downloads exactly those bytes or nothing.

## What `realstop setup` does

1. Only for the default folder `~/.claude/realstop/model/realstop-quit` and only when the judge is `local` (the default) or `auto`, and the model is missing or not verified yet (a model whose checksums match is not downloaded again).
2. Runs `scripts/fetch_model.py`, which reads the pinned list `scripts/model-files.txt` (sha256, byte size, relative path for each file).
3. If any value in the list is still TBD, or `MODEL_REVISION` is not a pinned 40-hex commit hash, it stops with exit 3 before touching the network.
4. Otherwise it downloads each file from `https://huggingface.co/<MODEL_REPO>/resolve/<MODEL_REVISION>/<path>` into a temporary folder next to the destination, checks size and sha256 of every file, and only then moves the folder into place. A mismatch, a short read, a network error or an unsafe path leaves nothing installed and exits 1.
5. Any non-zero exit is non-fatal for the install: it prints that the phrase rules are active and carries on. The hook already falls back to the rules whenever the model or its server is missing.

The single constant is `MODEL_REPO` in `scripts/fetch_model.py` (`decision-tune/realstop`), next to `MODEL_REVISION` (pin it to a commit hash so the bytes cannot move under the sha256 values).

Options: `realstop judge rules` never fetches (`--no-download` and `REALSTOP_NO_DOWNLOAD` are gone). `--model DIR` uses a folder you manage yourself, which install never writes to. To fetch by hand: `python3 scripts/fetch_model.py --dest ~/.claude/realstop/model/realstop-quit`.

## Privacy and integrity

The request goes to Hugging Face and carries no data from your machine. The sha256 values live in this git repo, not on the download host, so a changed or corrupted file is refused even if the host serves it. After install, `pack.json` (the question and class mixes) is hashed into the install manifest and checked on every stop.

## Serving it

`realstop serve` (it runs `scripts/realstop-serve.sh`) runs `decision-tune serve` (Apple silicon, MLX) on a private loopback port behind a small loopback proxy. It uses `DT_PY` or the user-local venv `~/.claude/realstop/venv`, and creates that venv with `pip install "decision-tune[mlx]==1.0.0"` on first use (network, about 1 GB). See the header of the script.

## License

The base model is DecisionTune 1.0 (Apache-2.0). The fine-tuned pack is also Apache-2.0, the same license as its base model and as RealStop's code.

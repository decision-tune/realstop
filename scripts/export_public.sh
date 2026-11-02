#!/bin/bash
# Build the public repository: a fresh git repo with ONE commit, holding exactly the files this repo tracks.
#   bash scripts/export_public.sh [DEST]
# Steps: export the committed tree of HEAD (`git archive`, so never .git and never uncommitted edits); git init; run scripts/leak_check.py and
# tests/run.sh inside the export; commit once as "RealStop 0.1.1 research preview". It never adds a remote or pushes.
# DEST may exist only as an earlier export (a single commit with that subject); anything else there is refused.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-${REALSTOP_EXPORT_DIR:-}}"
[ -n "$dest" ] || { echo "export: give DEST (or set REALSTOP_EXPORT_DIR)" >&2; exit 2; }
msg="RealStop 0.1.1 research preview"

cd "$here"
if [ -n "$(git status --porcelain)" ]; then
  echo "export: note: uncommitted changes are NOT exported (HEAD is):" >&2; git status --short >&2
fi
src_head="$(git rev-parse --short HEAD)"

if [ -e "$dest" ]; then
  # Replace only an earlier export of this script, never anything else.
  if [ -d "$dest/.git" ] && [ "$(git -C "$dest" rev-list --count HEAD 2>/dev/null)" = 1 ] \
     && [ "$(git -C "$dest" log -1 --format=%s 2>/dev/null)" = "$msg" ] && [ -z "$(git -C "$dest" remote)" ]; then
    rm -rf "$dest"
  else
    echo "export: $dest exists and is not an earlier export; not touching it" >&2; exit 1
  fi
fi
mkdir -p "$dest"

# Allowlist: the files tracked at HEAD, exactly as committed.
git archive --format=tar HEAD | tar -x -C "$dest"
n_src="$(git ls-tree -r --name-only HEAD | wc -l | tr -d ' ')"

cd "$dest"
git init -q -b main
git add -A
n_dst="$(git ls-files | wc -l | tr -d ' ')"
[ "$n_src" = "$n_dst" ] || { echo "export: file count differs: source $n_src, export $n_dst" >&2; exit 1; }

python3 scripts/leak_check.py "$dest"
bash tests/run.sh > "$dest.tests.log" 2>&1 || { echo "export: tests/run.sh failed in the export, see $dest.tests.log" >&2; exit 1; }
echo "export: tests/run.sh passed in the export: $(grep -c '^PASS' "$dest.tests.log") PASS, $(grep -c '^FAIL' "$dest.tests.log" || true) FAIL"
[ -z "$(git status --porcelain --untracked-files=all | grep -v '^A ' || true)" ] || { echo "export: the tests changed the export tree" >&2; git status --short >&2; exit 1; }

git commit -q -m "$msg"
echo "export: $dest at $(git rev-parse --short HEAD) ($n_dst files, from source $src_head)"

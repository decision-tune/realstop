#!/bin/bash
# Bootstrap for the `realstop` command line (the package in this folder). bash 3.2 and Python 3.9 safe.
#   bash install.sh [flags]    installs the package (uv tool, else pipx, else python3 -m pip install --user) and runs
#                              `realstop setup` with your flags. The old flags still work and map to the new commands:
#     --judge phrases|jev|local|auto, --strength S, --port N, --model DIR, --key-stdin   ->  realstop setup <same flags>
#     --keep-server                                                                       ->  realstop setup --keep-server
#     --pause | --resume | --diagnose | --uninstall | --uninstall-server                  ->  realstop pause | resume | diagnose
#                                                                                         |  uninstall | serve --unkeep
#   --no-download (and REALSTOP_NO_DOWNLOAD) are gone: `realstop judge rules` is "no model", and setup does not download
#   the model again when its checksums match.
#   REALSTOP_FROM_SOURCE=1         run the command straight from this folder without installing the package (tests, development).
# Everything else (menu, files, settings edits, backups, manifest, migration) lives in the package: see `realstop --help`.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
py=/usr/bin/python3
[ -x "$py" ] || py="$(command -v python3 || true)"
[ -n "$py" ] && "$py" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null || { echo "install: need python3 >= 3.9" >&2; exit 1; }

mode=setup; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --pause) mode=pause; shift ;;
    --resume) mode=resume; shift ;;
    --diagnose) mode=diagnose; shift ;;
    --uninstall) mode=uninstall; shift ;;
    --uninstall-server) mode=unkeep; shift ;;
    --no-download) echo "install: --no-download is gone. For no model use: realstop judge rules" >&2; exit 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
case "$mode" in
  pause|resume|diagnose|uninstall) cmd=("$mode") ;;
  unkeep) cmd=(serve --unkeep) ;;
  setup) cmd=(setup) ;;
esac
# setup keeps the flags it was given; every other mode takes none of the old setup flags (the command rejects them)
if [ ${#args[@]} -gt 0 ]; then cmd=("${cmd[@]}" "${args[@]}"); fi

from_source() { PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "$py" -m realstop "${cmd[@]}"; }
# only setup installs the package; the other modes run from this folder, so --uninstall/--pause never need a network
if [ "$mode" != setup ] || [ -n "${REALSTOP_FROM_SOURCE:-}" ]; then from_source; fi

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$here" >&2
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$here" >&2
else
  "$py" -m pip install --user --upgrade "$here" >&2 || { echo "install: pip could not install the package (try: uv tool install . or pipx install .)" >&2; exit 1; }
  exec "$py" -m realstop "${cmd[@]}"
fi
if command -v realstop >/dev/null 2>&1; then exec realstop "${cmd[@]}"; fi
for d in "$HOME/.local/bin"; do
  [ -x "$d/realstop" ] && { echo "note: $d is not on your PATH yet (uv tool update-shell, or pipx ensurepath)" >&2; exec "$d/realstop" "${cmd[@]}"; }
done
echo "install: the package installed but the realstop command is not on PATH; run: uv tool update-shell (or pipx ensurepath), then realstop setup" >&2
exit 1

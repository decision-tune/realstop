#!/bin/bash
# Serve the local DecisionTune quit judge for hooks/no-quit.py, on loopback only, in the foreground (Ctrl-C stops it).
#   realstop serve (or: bash scripts/realstop-serve.sh)   model_dir and port from ~/.claude/realstop/config.json
#   realstop serve --model DIR --port N
# Python with decision-tune (found in this order):
#   1. DT_PY=/path/to/python           an interpreter you already prepared (decision-tune installed in it)
#   2. ~/.claude/realstop/venv        the user-local venv this script creates (REALSTOP_HOME overrides the folder)
#   3. otherwise the script creates that venv with the first Python >= 3.10 it finds (REALSTOP_BASE_PYTHON picks one)
#      and runs `pip install "decision-tune[mlx]==1.0.0"` in it (plain `decision-tune` off Apple silicon). That needs
#      the network once and about 1 GB of disk (torch). REALSTOP_NO_INSTALL=1 turns the install off: the script then
#      exits 2 with the command to run, and the hook keeps using the phrase rules. Nothing is installed outside the venv.
# Starts scripts/realstop_server.py (the pinned decision-tune package, MLX on Apple silicon; it answers /decide as `decision-tune serve` did, plus both heads and /embed for `realstop learn`) on a private loopback port and a small loopback proxy on the
# public port. One request at a time. If the optional `dtpack` package is importable (a GPU-lease convention for people
# who train on the same GPU: /tmp/dt-gpu.lock), the proxy takes that lease per request and answers 503 at once when a
# training job holds it, so the hook falls back to the phrase rule for that stop. Without dtpack there is no lease.
# Ceiling: the model weights (1.5 GB on disk) stay resident in GPU memory between requests, which no lease covers; stop
# the server (Ctrl-C, or realstop serve --unkeep) before a memory-hungry job. Upgrade path: a lease-free
# Core ML or CPU backend once one is fast enough for a Stop hook (torch CPU: 1.1 s per call).
# Lifetime: the proxy exits (code 1) when the model process exits, so launchd's KeepAlive restarts both; the proxy kills
# its model child on any exit it can catch. After a SIGKILL of the proxy the model is orphaned, so the proxy records the
# model pid in $REALSTOP_HOME/model-server.pid (default ~/.claude/realstop) and kills that stale pid, if it is still a
# decision-tune process, before it starts a new one. Ceiling: the pid check is by command name, not a lock.
# Foreground by default (Ctrl-C stops it); realstop serve --keep runs this same script as a user LaunchAgent.
# The hook falls back to the phrase rule whenever nothing answers on the port.
set -euo pipefail
rhome="${REALSTOP_HOME:-${RESILIENT_HOME:-$HOME/.claude/realstop}}"
venv="$rhome/venv"
py="${DT_PY:-}"
cfg="$HOME/.claude/realstop/config.json"
model=""; port=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) model="${2:-}"; shift 2 ;;
    --port) port="${2:-}"; shift 2 ;;
    *) echo "realstop-serve: unknown argument $1" >&2; exit 2 ;;
  esac
done
if [ -f "$cfg" ]; then  # fill what the flags left out from the installed config (realstop judge local)
  [ -n "$model" ] || model="$(/usr/bin/python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("model_dir") or "")' "$cfg")"
  [ -n "$port" ] || port="$(/usr/bin/python3 -c 'import json, sys, urllib.parse; print(urllib.parse.urlsplit(json.load(open(sys.argv[1])).get("local_url") or "").port or "")' "$cfg")"
fi
port="${port:-8765}"
[ -n "$model" ] || { echo "realstop-serve: no model dir (pass --model or run realstop judge local)" >&2; exit 2; }
[ -f "$model/pack.json" ] || { echo "realstop-serve: no pack.json in $model" >&2; exit 2; }
case "$port" in *[!0-9]*|"") echo "realstop-serve: --port must be a number" >&2; exit 2 ;; esac
if [ -z "$py" ]; then  # find the user-local venv, or build it
  if [ ! -x "$venv/bin/decision-tune" ]; then
    [ -z "${REALSTOP_NO_INSTALL:-}" ] || { echo "realstop-serve: decision-tune is not installed (no $venv/bin/decision-tune). Run: python3.12 -m venv $venv && $venv/bin/pip install 'decision-tune[mlx]==1.0.0'  (or set DT_PY)" >&2; exit 2; }
    base="${REALSTOP_BASE_PYTHON:-}"
    if [ -z "$base" ]; then
      for c in python3.13 python3.12 python3.11 python3.10 python3; do
        b="$(command -v "$c" 2>/dev/null)" || continue
        "$b" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null && { base="$b"; break; }
      done
    fi
    [ -n "$base" ] || { echo "realstop-serve: need Python 3.10+ to install decision-tune (brew install python@3.12, then retry, or set DT_PY)" >&2; exit 2; }
    spec="${REALSTOP_PIP_SPEC:-decision-tune==1.0.0}"
    if [ -z "${REALSTOP_PIP_SPEC:-}" ] && [ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ]; then spec="decision-tune[mlx]==1.0.0"; fi
    echo "realstop-serve: installing $spec into $venv (first run only, needs the network)" >&2
    mkdir -p "$rhome"; pre=no; [ -e "$venv" ] && pre=yes  # only remove a venv this run created
    "$base" -m venv "$venv" && "$venv/bin/python" -m pip install --quiet --disable-pip-version-check ${REALSTOP_PIP_ARGS:-} "$spec" \
      || { echo "realstop-serve: installing decision-tune failed (the phrase rules keep working); remove $venv and retry, or set DT_PY" >&2; [ "$pre" = yes ] || rm -rf "$venv"; exit 2; }
    [ -x "$venv/bin/decision-tune" ] || { echo "realstop-serve: decision-tune was installed but its command is missing" >&2; [ "$pre" = yes ] || rm -rf "$venv"; exit 2; }
  fi
  py="$venv/bin/python"
fi
[ -x "$py" ] || { echo "realstop-serve: interpreter not found: $py (set DT_PY)" >&2; exit 2; }
exec "$py" - "$model" "$port" "${REALSTOP_HOME:-${RESILIENT_HOME:-$HOME/.claude/realstop}}" "$(cd "$(dirname "$0")" && pwd)" <<'PY'
import http.server, os, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.request
os.environ.setdefault("DT_GPU_ABORT_LOG", "/dev/null")  # a busy GPU is the normal case here, not an event to log per stop
try:  # optional GPU-lease convention (see the header); without it every request just runs
    from dtpack.gpu import GpuBusy, gpu_lease
except ImportError:
    from contextlib import contextmanager

    class GpuBusy(Exception):
        pass

    @contextmanager
    def gpu_lease(*a, **k):
        yield

model, port, state_dir, scripts_dir = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
s = socket.socket(); s.bind(("127.0.0.1", 0)); inner = s.getsockname()[1]; s.close()
cli = os.environ.get("DT_CLI") or os.path.join(os.path.dirname(sys.executable), "decision-tune")  # DT_CLI: tests only
pidfile = os.path.join(state_dir, "model-server.pid")
try:  # a model orphaned by a SIGKILLed proxy: stop it before starting another copy
    old = int(open(pidfile).read().strip())
    cmd = subprocess.run(["/bin/ps", "-p", str(old), "-o", "command="], capture_output=True, text=True).stdout
    if old > 1 and (os.path.basename(cli) in cmd or "realstop_server.py" in cmd) and "serve" in cmd:
        os.kill(old, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(old, 0)
            except OSError:
                break
        else:
            os.kill(old, signal.SIGKILL)
except (OSError, ValueError):
    pass
if os.environ.get("DT_CLI"):  # tests only: a stand-in for the model process
    child = [cli, "serve", "--host", "127.0.0.1", "--model", model, "--port", str(inner), "--backend", "mlx"]
else:  # RealStop's own server on the pinned decision-tune package: score, both heads and the vector in one forward pass
    child = [sys.executable, os.path.join(scripts_dir, "realstop_server.py"),
             "--host", "127.0.0.1", "--model", model, "--port", str(inner), "--backend", "mlx", "--state-dir", state_dir]
backend = subprocess.Popen(child)
os.makedirs(state_dir, exist_ok=True)
open(pidfile, "w").write("%d\n" % backend.pid)
one_at_a_time = threading.Lock()
stopping = threading.Event()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never through http_proxy


class H(http.server.BaseHTTPRequestHandler):
    def _reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _forward(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (inner, self.path), data=self.rfile.read(n) if n else None,
                                     method=self.command, headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with opener.open(req, timeout=120) as r:  # /embed batches take several seconds
                self._reply(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._reply(e.code, e.read())
        except Exception:
            self._reply(502, b'{"error": "model server not ready"}')

    def do_GET(self):
        self._forward()

    def do_POST(self):
        with one_at_a_time:
            try:
                with gpu_lease("realstop-serve-request", poll=0):
                    self._forward()
            except GpuBusy:
                self._reply(503, b'{"error": "GPU busy"}')

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
def stop(*a):
    stopping.set(); backend.terminate(); threading.Thread(target=httpd.shutdown, daemon=True).start()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGHUP, stop)
code = 0
def watch():  # the model died: take the proxy down too, so launchd restarts both
    global code
    backend.wait()
    if not stopping.is_set():
        code = 1
        httpd.shutdown()
threading.Thread(target=watch, daemon=True).start()
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    stopping.set()
    backend.terminate()
    try:
        backend.wait(10)
    except Exception:
        backend.kill()
    try:
        os.unlink(pidfile)
    except OSError:
        pass
sys.exit(code)
PY

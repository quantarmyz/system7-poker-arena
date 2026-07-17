"""System 7 — background-job backend (systemd OR plain subprocess).

The dashboard launches/stops/reads training runs. On the LXC that is done with
systemd (`systemd-run`/`systemctl`/`journalctl`); inside Docker there is no systemd,
so the same operations fall back to plain subprocesses with per-job log files and a
small JSON registry under S7_JOBS_DIR.

Backend auto-detected (override with S7_RUN_BACKEND=systemd|subprocess):
  systemd     -> /run/systemd/system exists and `systemd-run` on PATH
  subprocess  -> everything else (Docker, bare host, CI)

Public API (backend-agnostic):
  launch(label, argv, env=None) -> unit id      pyrun(script, *args) -> argv
  stop(label)        cleanup(label)             list_jobs() -> [{unit,label,state}]
  logs(unit, n)      is_active(name) -> str
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.environ.get("S7_JOBS_DIR", os.path.join(HERE, ".jobs"))
_SYSTEMD_PATH = "/usr/local/bin:/usr/bin:/bin"
_PROCS = {}                       # label -> Popen (best-effort reaping within one dash process)


def _detect():
    b = os.environ.get("S7_RUN_BACKEND", "").strip().lower()
    if b in ("systemd", "subprocess"):
        return b
    if os.path.isdir("/run/systemd/system") and shutil.which("systemd-run"):
        return "systemd"
    return "subprocess"


BACKEND = _detect()


def _uv():
    return os.environ.get("S7_UV") or shutil.which("uv") or "/usr/local/bin/uv"


def pyrun(script, *args):
    """argv that runs a project python script with its deps (uv run if available)."""
    u = _uv()
    if shutil.which("uv") or os.path.exists(u):
        return [u, "run", script, *[str(a) for a in args]]
    return [sys.executable, script, *[str(a) for a in args]]


def _label(name):
    return str(name).replace("arena-run-", "").replace(".service", "")


def _meta(label):
    try:
        with open(os.path.join(JOBS_DIR, label + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _alive(pid, argv=None):
    """True only if the process exists AND is not a zombie (a killed-but-unreaped
    child still answers kill(pid,0), so check /proc state on Linux) AND — when the
    job argv is known — its cmdline still matches the job (pids get recycled when
    the container restarts; a stale registry must not mark random processes active)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            state = f.read().rsplit(") ", 1)[1].split(" ", 1)[0]
        if state in ("Z", "X", "x"):
            return False
    except FileNotFoundError:
        return False
    except Exception:
        try:
            os.kill(pid, 0)
        except Exception:
            return False
    if argv:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            script = next((a for a in argv if str(a).endswith(".py")), None)
            if script and os.path.basename(str(script)) not in cmd:
                return False
        except FileNotFoundError:
            return False
        except Exception:
            pass
    return True


def _reap():
    """Reap finished children launched by this process so they don't linger as zombies."""
    for label, p in list(_PROCS.items()):
        try:
            if p.poll() is not None:
                _PROCS.pop(label, None)
        except Exception:
            _PROCS.pop(label, None)


def launch(label, argv, env=None):
    """Start `argv` as a tracked background job named <label>. Returns the unit id.
    Raises on failure. `env` = extra vars (the run inherits the dashboard env too)."""
    env = {k: str(v) for k, v in (env or {}).items()}
    if BACKEND == "systemd":
        unit = "arena-run-" + label
        cmd = ["systemd-run", "--unit=" + unit, "--working-directory=" + HERE,
               "--setenv=HOME=" + HERE, "--setenv=PATH=" + _SYSTEMD_PATH,
               "--setenv=PYTHONUNBUFFERED=1"]
        cmd += ["--setenv=%s=%s" % (k, v) for k, v in env.items()]
        cmd += list(argv)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "systemd-run falló")[:300])
        return unit
    # subprocess backend (Docker / bare host)
    os.makedirs(JOBS_DIR, exist_ok=True)
    full_env = dict(os.environ)
    full_env["PYTHONUNBUFFERED"] = "1"
    full_env.update(env)
    logf = open(os.path.join(JOBS_DIR, label + ".log"), "ab", buffering=0)
    try:
        p = subprocess.Popen(list(argv), cwd=HERE, env=full_env,
                             stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        logf.close()
    safe_env = {k: ("***" if ("KEY" in k or "TOKEN" in k or "SECRET" in k) else v)
                for k, v in env.items()}           # no persistir secretos en disco
    with open(os.path.join(JOBS_DIR, label + ".json"), "w", encoding="utf-8") as f:
        json.dump({"label": label, "pid": p.pid, "argv": list(argv),
                   "started": time.time(), "env": safe_env}, f)
    _PROCS[label] = p
    return "arena-run-" + label


def stop(name):
    """Terminate a job (whole process group). Accepts unit or bare label."""
    label = _label(name)
    if BACKEND == "systemd":
        unit = name if str(name).startswith("arena-") else "arena-run-" + label
        try:
            subprocess.run(["systemctl", "stop", unit], timeout=10)
        except Exception:
            pass
        return True
    meta = _meta(label)
    pid = (meta or {}).get("pid")
    if pid:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except Exception:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass
    _PROCS.pop(label, None)
    return True


def cleanup(label):
    """Remove a finished job's bookkeeping (reset-failed on systemd; rm registry/log here)."""
    label = _label(label)
    if BACKEND == "systemd":
        try:
            subprocess.run(["systemctl", "reset-failed", "arena-run-" + label], timeout=5)
        except Exception:
            pass
        return
    for ext in (".json", ".log"):
        try:
            os.remove(os.path.join(JOBS_DIR, label + ext))
        except Exception:
            pass
    _PROCS.pop(label, None)


def list_jobs():
    """All arena-run-* jobs as [{unit,label,state}] (state: active|inactive|failed)."""
    if BACKEND == "systemd":
        out = []
        try:
            r = subprocess.run(["systemctl", "list-units", "--type=service", "--all", "--no-legend",
                                "--plain", "arena-run-*"], capture_output=True, text=True, timeout=4).stdout
            for line in r.splitlines():
                parts = line.split()
                if parts and parts[0].startswith("arena-run-"):
                    out.append({"unit": parts[0], "label": _label(parts[0]),
                                "state": parts[2] if len(parts) > 2 else "?"})
        except Exception:
            pass
        return out
    _reap()
    out = []
    try:
        for fn in sorted(os.listdir(JOBS_DIR)):
            if not fn.endswith(".json"):
                continue
            label = fn[:-5]
            meta = _meta(label) or {}
            pid = meta.get("pid")
            out.append({"unit": "arena-run-" + label, "label": label,
                        "state": "active" if (pid and _alive(pid, meta.get("argv"))) else "inactive"})
    except FileNotFoundError:
        pass
    return out


def logs(unit, n=80):
    """Tail a job's output (journalctl on systemd; the log file otherwise)."""
    if BACKEND == "systemd":
        try:
            return subprocess.run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat"],
                                  capture_output=True, text=True, timeout=5).stdout
        except Exception as e:
            return "error: " + str(e)
    try:
        with open(os.path.join(JOBS_DIR, _label(unit) + ".log"), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-int(n):])
    except Exception:
        return "(sin salida todavía)"


def is_active(name):
    """Status string for a service/job ('active'/'inactive'/'failed'/'n/a')."""
    if BACKEND == "systemd":
        try:
            return subprocess.run(["systemctl", "is-active", name],
                                  capture_output=True, text=True, timeout=3).stdout.strip() or "?"
        except Exception:
            return "?"
    meta = _meta(_label(name))
    if meta:
        return "active" if _alive(meta.get("pid", 0), meta.get("argv")) else "inactive"
    return "n/a"

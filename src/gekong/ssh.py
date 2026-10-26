"""OpenSSH plumbing: one ControlMaster per host, every call a new channel on it.

Each remote call runs a one-line python3 bootstrap that reads the helper source
(gekong/remote/helper.py) from stdin and execs it, so the remote needs nothing
installed and keeps nothing behind.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from functools import lru_cache
from importlib import resources

from . import paths

BOOTSTRAP = (
    "import sys;v=sys.version_info;"
    'v>=(3,6) or sys.exit("gekong: remote python3 is %d.%d, need >= 3.6"%v[:2]);'
    "b=sys.stdin.buffer;n=int(b.readline());"
    'exec(compile(b.read(n),"gekong-helper","exec"))'
)
# Ops that start the user's commands, or need what their rc files put on PATH (tmux).
FULL_ENV_OPS = ("run", "job_start", "job_ls", "job_tail", "job_kill", "wait")


def remote_cmd(op: str) -> str:
    """The remote command line. Its last word tells the user's ~/.bashrc whether the
    call runs their commands (gekong:run) or only reads, writes and searches files
    (gekong:call), so a slow rc file can skip its setup for the latter."""
    tag = "gekong:run" if op in FULL_ENV_OPS else "gekong:call"
    return f"exec python3 -c '{BOOTSTRAP}' {tag}"


class GekongError(Exception):
    exit_code = 1


class NotConnected(GekongError):
    exit_code = 255

    def __init__(self, host):
        super().__init__(
            f"gekong: not connected to {host}. Ask the user to run `gekong up {host}` "
            "(it may need an interactive login); do not retry or try another way to log in."
        )
        self.host = host


class RemoteError(GekongError):
    pass


def config_opts() -> list:
    cfg = os.environ.get("GEKONG_SSH_CONFIG")
    return ["-F", cfg] if cfg else []


def base_opts() -> list:
    """Options naming the master socket; %C is ssh's hash of host, user, port and jump."""
    return config_opts() + ["-o", "ControlPath=" + str(paths.ctl_dir() / "%C")]


def control_path(host: str, extra: list = ()) -> str:
    """The master socket for `host`, with %C expanded the way ssh itself does it."""
    r = subprocess.run(["ssh", *base_opts(), *extra, "-G", "--", host],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "controlpath" and value:
            return value
    raise RemoteError(f"gekong: cannot resolve the ssh configuration for {host}: {r.stderr.strip()}")


def client_opts(host: str, extra: list = ()) -> list:
    """Options for every non-master ssh: go through the master or fail.

    ControlMaster=no alone would fall back to a fresh connection if the master
    vanished after we checked it. With the socket path spelled out (so it no
    longer depends on ProxyJump through %C) a ProxyCommand that always fails
    can't affect the multiplexed path, and makes any direct connection fail.
    """
    return config_opts() + ["-o", "ControlPath=" + control_path(host, extra), "-o", "ControlMaster=no",
                            "-o", "BatchMode=yes", "-o", "ProxyCommand=false"]


def master_alive(host: str) -> bool:
    r = subprocess.run(["ssh", *base_opts(), "-O", "check", "--", host],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def require_master(host: str) -> None:
    paths.check_host(host)
    if not master_alive(host):
        raise NotConnected(host)


@lru_cache(maxsize=1)
def helper_source() -> bytes:
    return resources.files("gekong.remote").joinpath("helper.py").read_bytes()


def payload(req: dict, data: bytes = None) -> bytes:
    src = helper_source()
    if data is not None:
        req = {**req, "data_len": len(data)}
    return b"%d\n" % len(src) + src + json.dumps(req).encode() + b"\n" + (data or b"")


def helper_argv(host: str, op: str) -> list:
    return ["ssh", *client_opts(host), "-T", "--", host, remote_cmd(op)]


def _failure(host, rc, stderr: bytes) -> GekongError:
    if not master_alive(host):
        return NotConnected(host)
    msg = stderr.decode("utf-8", "replace").strip() or f"exit status {rc}"
    return RemoteError(f"gekong: remote call on {host} failed: {msg}")


def call(host: str, req: dict, data: bytes = None, timeout: float = None):
    """Run one helper op; return (response dict, response bytes).

    Raises NotConnected / RemoteError; a response with ok=False and a kind other
    than "error" (e.g. a write conflict) is returned to the caller. There is no
    master check up front: without a master the call fails closed (see
    client_opts) and the failure is diagnosed afterwards.
    """
    paths.check_host(host)
    try:
        p = subprocess.run(helper_argv(host, req["op"]), input=payload(req, data),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"gekong: remote call on {host} timed out after {timeout}s")
    out = p.stdout
    nl = out.find(b"\n")
    if p.returncode != 0 or nl < 0:
        raise _failure(host, p.returncode, p.stderr)
    try:
        resp = json.loads(out[:nl])
    except ValueError:
        raise _failure(host, p.returncode, p.stderr + out)
    body = out[nl + 1:]
    if "data_len" in resp:
        if len(body) < resp["data_len"]:
            raise RemoteError(f"gekong: truncated response from {host}")
        body = body[:resp["data_len"]]
    if not resp.get("ok") and resp.get("kind") == "error":
        raise RemoteError(f"gekong ({host}): {resp.get('error')}")
    return resp, body


def _pump(src, dst, log, on_broken=None):
    """Copy a pipe to our stdout/stderr (and the run log) as data arrives."""
    fd = src.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        if log is not None:
            log.write(chunk)
            log.flush()
        if dst is not None:
            try:
                dst.write(chunk)
                dst.flush()
            except (BrokenPipeError, ValueError):
                dst = None
                if on_broken is not None:  # e.g. `gk run ... | head`: stop like SIGPIPE would
                    on_broken()


def stream(host: str, req: dict, log=None, check: bool = True) -> int:
    """Run a streaming helper op with our stdout/stderr attached; return its exit code.

    ssh's stdin stays open for the whole call: the remote side treats EOF on it
    as "the local command is gone" and kills its process group. Closing it is
    therefore how SIGINT/SIGTERM/SIGHUP get to the remote, and if we are
    SIGKILLed the kernel closes it for us.
    """
    if check:
        require_master(host)
    proc = subprocess.Popen(helper_argv(host, req["op"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        proc.stdin.write(payload(req))
        proc.stdin.flush()
    except BrokenPipeError:
        pass

    def hang_up():
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    pumps = [threading.Thread(target=_pump, args=(proc.stdout, sys.stdout.buffer, log, hang_up), daemon=True),
             threading.Thread(target=_pump, args=(proc.stderr, sys.stderr.buffer, log), daemon=True)]
    for t in pumps:
        t.start()

    got = []

    def on_signal(signum, frame):
        got.append((signum, time.monotonic()))
        if len(got) == 1:
            hang_up()
        else:
            proc.kill()

    caught = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {s: signal.signal(s, on_signal) for s in caught}
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if got and time.monotonic() - got[0][1] > 15:
                    proc.kill()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    for t in pumps:
        t.join(timeout=5)
    hang_up()
    rc = proc.returncode
    if got:
        return 128 + got[0][0]
    if rc == 255 and not master_alive(host):
        sys.stderr.write(str(NotConnected(host)) + "\n")
    return rc

"""gekong: work on remote hosts over one OpenSSH master connection each.

Connections
  gekong up HOST              log in once (2FA etc.) and keep a master connection
  gekong down HOST | --all    close master connections
  gekong status [HOST]        show connections

Remote work
  gekong run [-C DIR] HOST COMMAND...
                              run a command; killing gekong kills it on the remote too
  gekong job start [-C DIR] [--name NAME] HOST COMMAND...
                              start a long job in remote tmux (survives disconnects)
  gekong job ls HOST          list jobs
  gekong job tail [-n N] [-f] HOST JOB
                              show a job's output (-f: follow until it ends)
  gekong job kill HOST JOB    stop a job and its whole process group
  gekong wait HOST (--job JOB | --file PATH) [--pattern REGEX] [--timeout SEC] [--new]
                              block until a line matches (exit 0), the timeout passes (1)
                              or the job ends without a match (2); with --job and no
                              pattern, wait for the job and exit with its code
  gekong ssh [ssh options] HOST [COMMAND]
                              plain ssh reusing the master: interactive shell,
                              rsync -e 'gekong ssh', GIT_SSH_COMMAND='gekong ssh'

Commands started by agents
  gekong ps [-a] [HOST]       list running run/wait/tail commands (-a: include finished ones)
  gekong tail [-n N] [-f] ID  show one's output
  gekong kill ID              stop one (its remote process group goes with it)

Setup
  gekong setup claude [--dry-run | --uninstall]
                              install hooks, permissions, MCP server and skill into Claude Code

HOST is passed to ssh verbatim (an ~/.ssh/config alias or user@hostname) and is
always explicit. A remote COMMAND runs in the remote login shell; several words
are joined with spaces, as ssh does.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

from . import cli_remote, paths, runs, ssh


def _record(host: str):
    return paths.hosts_dir() / f"{host}.json"


def known_hosts() -> list:
    d = paths.hosts_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _close(host: str) -> None:
    subprocess.run(["ssh", *ssh.base_opts(), "-O", "exit", "--", host],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _describe(info: dict) -> str:
    tools = ", ".join(t for t in ("rg", "tmux") if info.get(t)) or "no rg/tmux"
    return f"{info['hostname']}, python {info['python']}, {tools}"


def cmd_up(a) -> int:
    host = paths.check_host(a.host)
    if ssh.master_alive(host):
        print(f"gekong: already connected to {host}")
    else:
        paths.ensure_private(paths.ctl_dir())
        rc = subprocess.call(["ssh", *ssh.base_opts(), "-o", "ControlMaster=yes", "-o", "ControlPersist=yes",
                              "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
                              "-N", "-f", "--", host])
        if rc != 0 or not ssh.master_alive(host):
            print(f"gekong: could not connect to {host} (ssh exit status {rc})", file=sys.stderr)
            return 255
    try:
        info, _ = ssh.call(host, {"op": "hello"}, timeout=60)
    except ssh.GekongError as e:
        _close(host)
        print(f"{e}\ngekong: {host} needs python3 >= 3.6 on its PATH; disconnected.", file=sys.stderr)
        return 1
    if info["system"] != "Linux":
        _close(host)
        print(f"gekong: {host} runs {info['system']}; only Linux remotes are supported. Disconnected.",
              file=sys.stderr)
        return 1
    paths.ensure_private(paths.hosts_dir())
    _record(host).write_text(json.dumps({"host": host, "since": time.time(), "info": info}))
    print(f"gekong: connected to {host} ({_describe(info)})")
    print(f"  remote files: {paths.mirror_prefix(host)}/<absolute remote path>")
    return 0


def cmd_down(a) -> int:
    if a.all:
        hosts = known_hosts()
    elif a.host:
        hosts = [paths.check_host(a.host)]
    else:
        print("gekong down: give a HOST or --all", file=sys.stderr)
        return 2
    for host in hosts:
        if ssh.master_alive(host):
            _close(host)
            print(f"gekong: disconnected from {host}")
        else:
            print(f"gekong: {host} was not connected")
        _record(host).unlink(missing_ok=True)
    return 0


def cmd_status(a) -> int:
    hosts = [paths.check_host(a.host)] if a.host else known_hosts()
    if not hosts:
        print("gekong: no connections (start one with `gekong up HOST`)")
        return 0
    down = 0
    for host in hosts:
        if not ssh.master_alive(host):
            down += 1
            print(f"{host:<24} down       run `gekong up {host}`")
            continue
        t0 = time.monotonic()
        try:
            info, _ = ssh.call(host, {"op": "hello"}, timeout=30)
            ms = (time.monotonic() - t0) * 1000
            print(f"{host:<24} connected  {ms:.0f} ms per call; {_describe(info)}")
            if ms > 1000:
                print(f"{'':<24} slow remote shell start-up; to skip it for file reads, writes and searches, put\n"
                      f"{'':<24}   [[ $BASH_EXECUTION_STRING == *gekong:call* ]] && return\n"
                      f"{'':<24} at the top of ~/.bashrc on {host}")
        except ssh.GekongError as e:
            down += 1
            print(f"{host:<24} broken     {e}")
    return 1 if (a.host and down) else 0


def _age(t: float) -> str:
    s = int(time.time() - t)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def cmd_ps(a) -> int:
    rows = [m for m in runs.load_all()
            if (a.all or m["status"] == "running") and (not a.host or m["host"] == a.host)]
    if not rows:
        print("gekong: no running commands" + ("" if a.all else " (-a shows finished ones)"))
        return 0
    print(f"{'ID':<7} {'HOST':<18} {'STATUS':<10} {'AGE':>5}  COMMAND")
    for m in rows:
        cmd = " ".join(m["argv"])
        print(f"{m['id']:<7} {m['host']:<18} {m['status']:<10} {_age(m['started']):>5}  {cmd}")
    return 0


def _find(run_id: str) -> dict:
    try:
        return runs.find(run_id)
    except KeyError:
        raise SystemExit(f"gekong: no command with id {run_id!r} (see `gekong ps -a`)")


def cmd_tail(a) -> int:
    m = _find(a.id)
    out = sys.stdout.buffer
    with open(m["log"], "rb") as f:
        lines = f.read().splitlines(keepends=True)
        out.write(b"".join(lines[-a.lines:]) if a.lines > 0 else b"")
        out.flush()
        if not a.follow:
            return 0
        while True:
            chunk = f.read()
            if chunk:
                out.write(chunk)
                out.flush()
                continue
            if runs.find(a.id)["status"] != "running":
                out.write(f.read())
                out.flush()
                return 0
            time.sleep(0.3)


def cmd_kill(a) -> int:
    m = _find(a.id)
    if m["status"] != "running":
        print(f"gekong: {a.id} is not running ({m['status']})")
        return 0
    if not runs.same_process(m):
        print(f"gekong: pid {m['pid']} is no longer that command; not killing it", file=sys.stderr)
        return 1
    os.kill(m["pid"], signal.SIGTERM)
    for _ in range(100):
        if runs.find(a.id)["status"] != "running":
            print(f"gekong: stopped {a.id} ({' '.join(m['argv'])})")
            return 0
        time.sleep(0.1)
    print(f"gekong: sent SIGTERM to {a.id}, still running; try again to force it", file=sys.stderr)
    return 1


def cmd_setup(a) -> int:
    from . import setup_claude
    if a.target != "claude":
        print("gekong setup: only `claude` is supported", file=sys.stderr)
        return 2
    return setup_claude.uninstall() if a.uninstall else setup_claude.install(dry_run=a.dry_run)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gekong", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("up", help="connect")
    s.add_argument("host")
    s.set_defaults(fn=cmd_up)
    s = sub.add_parser("down", help="disconnect")
    s.add_argument("host", nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_down)
    s = sub.add_parser("status", help="show connections")
    s.add_argument("host", nargs="?")
    s.set_defaults(fn=cmd_status)
    for name, what in (("run", "run a command"), ("job", "long jobs in remote tmux"),
                       ("wait", "wait for a log line or a job"), ("ssh", "ssh reusing the master")):
        sub.add_parser(name, help=what, add_help=False)  # parsed by cli_remote; listed for --help
    s = sub.add_parser("ps", help="list commands started by agents")
    s.add_argument("host", nargs="?")
    s.add_argument("-a", "--all", action="store_true")
    s.set_defaults(fn=cmd_ps)
    s = sub.add_parser("tail", help="show a command's output")
    s.add_argument("id")
    s.add_argument("-n", type=int, default=50, dest="lines")
    s.add_argument("-f", action="store_true", dest="follow")
    s.set_defaults(fn=cmd_tail)
    s = sub.add_parser("kill", help="stop a command")
    s.add_argument("id")
    s.set_defaults(fn=cmd_kill)
    s = sub.add_parser("setup", help="integrate with an agent")
    s.add_argument("target", choices=["claude"])
    g = s.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    s.set_defaults(fn=cmd_setup)
    return p


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in cli_remote.COMMANDS:
        return cli_remote.main(args)
    a = parser().parse_args(args)
    try:
        return a.fn(a)
    except ssh.GekongError as e:
        print(e, file=sys.stderr)
        return e.exit_code
    except ValueError as e:
        print(f"gekong: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def entry():
    rc = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:  # reader went away (`gekong run ... | head`); don't print a traceback
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(rc)


if __name__ == "__main__":
    entry()

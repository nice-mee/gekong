"""`gekong ssh / run / job / wait`: work on a remote host over its master connection.

These commands take ssh-style or free-form arguments (a remote COMMAND may
contain anything), so they are parsed here by hand rather than by argparse;
cli_gekong.main hands them over before parsing the rest.
"""
import argparse
import os
import subprocess
import sys
import time

from . import paths, runs, ssh

SSH_VALUE_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")


class Usage(Exception):
    pass


def ssh_destination(args):
    """Find the destination in ssh-style args; return (its index or None, options that affect
    which master it maps to)."""
    keep = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (i + 1 if i + 1 < len(args) else None), keep
        if a.startswith("-") and len(a) > 1:
            for j in range(1, len(a)):
                c = a[j]
                if c in SSH_VALUE_OPTS:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = args[i] if i < len(args) else None
                    if c in "lpFoJ" and val is not None:
                        keep += ["-" + c, val]
                    break
            i += 1
            continue
        return i, keep
    return None, keep


def cmd_ssh(args):
    dest, keep = ssh_destination(args)
    if dest is not None and "-O" not in args:
        r = subprocess.run(["ssh", *ssh.base_opts(), *keep, "-O", "check", "--", args[dest]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            print(ssh.NotConnected(args[dest]), file=sys.stderr)
            return 255
        os.execvp("ssh", ["ssh", *ssh.client_opts(args[dest], keep), *args])
    os.execvp("ssh", ["ssh", *ssh.base_opts(), *args])  # no destination (e.g. -V) or -O ctl_cmd


def split_command(args, value_flags):
    """Parse `[options] HOST COMMAND...`; options may also follow HOST."""
    opts, host, i = {}, None, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            if host is None and i < len(args):
                host = args[i]
                i += 1
            break
        if a in value_flags:
            if i + 1 >= len(args):
                raise Usage(f"{a} needs a value")
            opts[value_flags[a]] = args[i + 1]
            i += 2
            continue
        if host is None:
            if a.startswith("-"):
                raise Usage(f"unknown option {a}")
            host = a
            i += 1
            continue
        break
    if host is None:
        raise Usage("missing HOST")
    if i >= len(args):
        raise Usage("missing COMMAND")
    return opts, paths.check_host(host), " ".join(args[i:])


def streamed(host, req, argv):
    """Run a streaming op, recorded for `gekong ps` / `gekong tail` / `gekong kill`."""
    ssh.require_master(host)
    run = runs.Run(host, argv)
    rc = None
    try:
        rc = ssh.stream(host, req, log=run.log, check=False)
    finally:
        run.finish(rc)
    return rc


def cmd_run(args):
    opts, host, cmd = split_command(args, {"-C": "cwd", "--cwd": "cwd"})
    return streamed(host, {"op": "run", "cmd": cmd, "cwd": opts.get("cwd")}, ["gekong", "run", *args])


def cmd_job(args):
    if not args or args[0] in ("-h", "--help"):
        raise Usage("expected start, ls, tail or kill")
    sub, rest = args[0], args[1:]
    if sub == "start":
        opts, host, cmd = split_command(rest, {"-C": "cwd", "--cwd": "cwd", "--name": "name"})
        resp, _ = ssh.call(host, {"op": "job_start", "cmd": cmd, "cwd": opts.get("cwd"), "name": opts.get("name")})
        jid = resp["id"]
        print(f"started job {jid} on {host} (tmux session {resp['session']}, log {resp['log']})")
        print(f"  output:  gekong job tail {host} {jid} [-f]")
        print(f"  wait:    gekong wait {host} --job {jid} [--pattern REGEX]")
        print(f"  stop:    gekong job kill {host} {jid}")
        return 0
    p = argparse.ArgumentParser(prog=f"gekong job {sub}")
    p.add_argument("host")
    if sub == "ls":
        a = p.parse_args(rest)
        resp, _ = ssh.call(paths.check_host(a.host), {"op": "job_ls"})
        jobs = resp["jobs"]
        if not jobs:
            print(f"no jobs on {a.host}")
            return 0
        print(f"{'ID':<8} {'NAME':<14} {'STATUS':<10} {'STARTED':<17} COMMAND")
        for j in jobs:
            st = j["status"] + (f" {j['code']}" if j["status"] == "exited" else "")
            started = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["started"]))
            print(f"{j['id']:<8} {(j.get('name') or '-'):<14} {st:<10} {started:<17} {j['cmd']}")
        return 0
    p.add_argument("job")
    if sub == "tail":
        p.add_argument("-n", type=int, default=50, dest="lines")
        p.add_argument("-f", action="store_true", dest="follow")
        a = p.parse_args(rest)
        req = {"op": "job_tail", "job": a.job, "lines": a.lines, "follow": a.follow}
        host = paths.check_host(a.host)
        if a.follow:
            return streamed(host, req, ["gekong", "job", *args])
        return ssh.stream(host, req)
    if sub == "kill":
        a = p.parse_args(rest)
        resp, _ = ssh.call(paths.check_host(a.host), {"op": "job_kill", "job": a.job})
        print(f"job {resp['id']}: " + ("killed" if resp["was"] == "running" else f"already {resp['was']}"))
        return 0
    raise Usage(f"unknown job command {sub!r}")


def cmd_wait(args):
    p = argparse.ArgumentParser(prog="gekong wait")
    p.add_argument("host")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--job")
    src.add_argument("--file")
    p.add_argument("--pattern")
    p.add_argument("--timeout", type=float)
    p.add_argument("--new", action="store_true", help="only match lines written after gekong wait starts")
    a = p.parse_args(args)
    if a.file and not a.pattern:
        raise Usage("--file needs --pattern")
    req = {"op": "wait", "job": a.job, "file": a.file, "pattern": a.pattern, "timeout": a.timeout, "new": a.new}
    return streamed(paths.check_host(a.host), req, ["gekong", "wait", *args])


COMMANDS = {"ssh": cmd_ssh, "run": cmd_run, "job": cmd_job, "wait": cmd_wait}


def main(args) -> int:
    """Run one of COMMANDS: args[0] is its name."""
    try:
        return COMMANDS[args[0]](args[1:])
    except Usage as e:
        print(f"gekong {args[0]}: {e} (see `gekong --help`)", file=sys.stderr)
        return 2
    except ssh.GekongError as e:
        print(e, file=sys.stderr)
        return e.exit_code
    except ValueError as e:
        print(f"gekong: {e}", file=sys.stderr)
        return 2

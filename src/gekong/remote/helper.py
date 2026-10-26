# gekong remote helper.
#
# Sent over stdin on every call (see gekong.ssh) and exec'd by a one-line
# python3 bootstrap, so nothing is installed on the remote host. It must stay
# compatible with python 3.6 and use only the standard library.
#
# Protocol: after the bootstrap has consumed the source, stdin carries one JSON
# request line, optionally followed by `data_len` raw bytes. Most ops answer with
# one JSON line on stdout (plus `data_len` raw bytes when present). Streaming ops
# (run, job_tail, wait) write raw output and report through their exit status;
# they treat EOF on stdin as "the local side is gone" and clean up.

import collections
import fcntl
import hashlib
import json
import os
import random
import re
import select
import shutil
import signal
import stat
import string
import subprocess
import sys
import tempfile
import time

IN = sys.stdin.buffer
OUT = sys.stdout.buffer

KILL_GRACE = 5.0
SCRATCH_MAX_AGE = 24 * 3600


class UserError(Exception):
    pass


def reply(obj, data=None):
    if data is not None:
        obj["data_len"] = len(data)
    OUT.write(json.dumps(obj).encode("utf-8") + b"\n")
    if data is not None:
        OUT.write(data)
    OUT.flush()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def expand(path):
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        raise UserError("path must be absolute: %r" % path)
    return os.path.normpath(p)


def read_exact(n):
    data = IN.read(n)
    if len(data) != n:
        raise UserError("short request body: got %d of %d bytes" % (len(data), n))
    return data


def rand_id(n=6):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def scratch_dir():
    d = "/tmp/gekong-%d" % os.getuid()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise UserError("refusing to use %s: not a directory owned by you" % d)
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def stdin_closed(timeout):
    """Wait up to `timeout` seconds; True once the local side has gone away."""
    try:
        ready, _, _ = select.select([0], [], [], timeout)
    except (InterruptedError, OSError):
        return False
    if not ready:
        return False
    try:
        return not os.read(0, 65536)
    except OSError:
        return True


def shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------- hello


def op_hello(req):
    u = os.uname()
    reply({
        "ok": True,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "system": u.sysname,
        "hostname": u.nodename,
        "uid": os.getuid(),
        "home": os.path.expanduser("~"),
        "rg": bool(shutil.which("rg")),
        "tmux": bool(shutil.which("tmux")),
    })


# ---------------------------------------------------------------- files


def op_read(req):
    path = expand(req["path"])
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return reply({"ok": True, "exists": False})
    if stat.S_ISDIR(st.st_mode):
        return reply({"ok": True, "exists": True, "kind": "dir"})
    if not stat.S_ISREG(st.st_mode):
        return reply({"ok": True, "exists": True, "kind": "special"})
    max_bytes = req.get("max_bytes")
    if max_bytes is not None and st.st_size > max_bytes:
        return reply({"ok": True, "exists": True, "kind": "file", "too_large": True, "size": st.st_size})
    with open(path, "rb") as f:
        data = f.read()
    sha = sha_bytes(data)
    resp = {"ok": True, "exists": True, "kind": "file", "sha": sha, "size": len(data)}
    if sha == req.get("have"):
        return reply(resp)
    reply(resp, data)


def file_state(target):
    """(sha256, stat) of a file, or (None, None) if it doesn't exist."""
    try:
        with open(target, "rb") as f:
            return sha_bytes(f.read()), os.fstat(f.fileno())
    except FileNotFoundError:
        return None, None
    except IsADirectoryError:
        raise UserError("%s is a directory" % target)


def op_write(req):
    """Compare-and-swap write: replace the file only if it still hashes to `expect`
    (None meaning "must not exist"), via a temp file renamed over it.

    Writers going through gekong are serialized by a lock on the parent directory.
    Other programs don't take that lock, so the hash is checked again right before
    the rename; only that instant stays uncovered, as POSIX has no conditional
    rename."""
    data = read_exact(req["data_len"])
    target = os.path.realpath(expand(req["path"]))
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        if req.get("expect") is not None:
            return reply({"ok": False, "kind": "conflict", "sha": None})
        os.makedirs(parent)
    lock = os.open(parent, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write_locked(req, data, target, parent)
    finally:
        os.close(lock)


def write_locked(req, data, target, parent):
    current, st = file_state(target)
    if current != req.get("expect"):
        return reply({"ok": False, "kind": "conflict", "sha": current})
    new = sha_bytes(data)
    if current == new:
        return reply({"ok": True, "sha": new, "unchanged": True})
    tmp = os.path.join(parent, ".%s.gekong-%s" % (os.path.basename(target), rand_id()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if st is None else 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        if st is not None:
            os.fchmod(fd, stat.S_IMODE(st.st_mode))
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.fsync(fd)
        os.close(fd)
        fd = None
        again, _ = file_state(target)
        if again != current:
            os.unlink(tmp)
            return reply({"ok": False, "kind": "conflict", "sha": again})
        os.rename(tmp, target)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reply({"ok": True, "sha": new})


# ---------------------------------------------------------------- output truncation


class Collector(object):
    """Keeps the head and tail of a long output; past `limit` characters the full
    output is spilled to a file in the scratch dir so it can be searched later."""

    def __init__(self, limit=20000, head=12000, tail=8000, line_max=2000):
        self.limit, self.head_budget, self.tail_budget, self.line_max = limit, head, tail, line_max
        self.lines = []
        self.chars = 0
        self.total_lines = 0
        self.spill = None
        self.spill_path = None
        self.head = []
        self.tail = collections.deque()
        self.tail_chars = 0

    def _clip(self, line):
        if len(line) > self.line_max:
            return line[:self.line_max] + " ... [line truncated]"
        return line

    def add(self, line):
        self.total_lines += 1
        self.chars += len(line) + 1
        if self.spill is None:
            self.lines.append(line)
            if self.chars > self.limit:
                self._start_spill()
            return
        self.spill.write(line + "\n")
        self._push_tail(line)

    def _push_tail(self, line):
        line = self._clip(line)
        self.tail.append(line)
        self.tail_chars += len(line) + 1
        while self.tail_chars > self.tail_budget and len(self.tail) > 1:
            self.tail_chars -= len(self.tail.popleft()) + 1

    def _start_spill(self):
        d = scratch_dir()
        now = time.time()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.startswith("out-") and now - os.lstat(p).st_mtime > SCRATCH_MAX_AGE:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.spill_path = os.path.join(d, "out-%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), rand_id(4)))
        self.spill = open(self.spill_path, "w", encoding="utf-8", errors="replace")
        used = 0
        rest = 0
        for line in self.lines:
            self.spill.write(line + "\n")
            clipped = self._clip(line)
            if rest == 0 and used + len(clipped) + 1 <= self.head_budget:
                self.head.append(clipped)
                used += len(clipped) + 1
            else:
                rest = 1
                self._push_tail(line)
        self.lines = None

    def result(self, prefix=""):
        if self.spill is None:
            return "\n".join(self._clip(l) for l in self.lines)
        self.spill.close()
        omitted = self.total_lines - len(self.head) - len(self.tail)
        marker = ("... [%d lines omitted; the full output (%d lines) is saved at %s%s"
                  " - Grep it or Read a line range of it instead of re-running the search] ..."
                  % (omitted, self.total_lines, prefix, self.spill_path))
        return "\n".join(self.head + [marker] + list(self.tail))


# ---------------------------------------------------------------- grep


def expand_braces(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[:m.start()] + alt + pattern[m.end():]))
    return out


def rg_argv(req, mode, root, pattern):
    argv = ["rg", "--no-config", "--color=never", "--no-heading", "--with-filename",
            "--hidden", "--glob=!.git", "--max-columns=500"]
    if req.get("-i"):
        argv.append("-i")
    if mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif mode == "count":
        argv.append("--count")
    else:
        if req.get("-n", True):
            argv.append("-n")
        argv += context_flags(req)
    if req.get("multiline"):
        argv += ["-U", "--multiline-dotall"]
    for g in (req.get("glob") or "").split():
        argv += ["--glob", g]
    if req.get("type"):
        argv += ["--type", req["type"]]
    return argv + ["-e", pattern, "--", root]


def grep_argv(req, mode, root, pattern, syntax):
    if req.get("type"):
        raise UserError("the `type` filter needs ripgrep (rg) on the remote host; use `glob` instead")
    if req.get("multiline"):
        raise UserError("`multiline` needs ripgrep (rg) on the remote host")
    argv = ["grep", "-r", "-I", "-H", "--color=never", "--exclude-dir=.git", syntax]
    if req.get("-i"):
        argv.append("-i")
    if mode == "files_with_matches":
        argv.append("-l")
    elif mode == "count":
        argv.append("-c")
    else:
        if req.get("-n", True):
            argv.append("-n")
        argv += context_flags(req)
    for g in (req.get("glob") or "").split():
        if "/" in g:
            raise UserError("glob %r: only file-name globs work without ripgrep on the remote" % g)
        argv += ["--include=" + p for p in expand_braces(g)]
    return argv + ["-e", pattern, "--", root]


def context_flags(req):
    out = []
    for k in ("-A", "-B", "-C"):
        if req.get(k) is not None:
            out += [k, str(int(req[k]))]
    if req.get("-C") is None and req.get("context") is not None:
        out += ["-C", str(int(req["context"]))]
    return out


def run_search(argv):
    """Start a search tool; stderr goes to a temp file so it can't block stdout."""
    errf = tempfile.TemporaryFile()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf)
    except OSError as e:
        errf.close()
        raise UserError("cannot run %s: %s" % (argv[0], e))
    return p, errf


def op_grep(req):
    pattern = req["pattern"]
    root = expand(req.get("path") or "~")
    mode = req.get("output_mode") or "files_with_matches"
    if mode not in ("files_with_matches", "content", "count"):
        raise UserError("unknown output_mode %r" % mode)
    prefix = req.get("prefix") or ""
    use_rg = bool(shutil.which("rg")) and not req.get("no_rg")
    if use_rg:
        attempts = [rg_argv(req, mode, root, pattern)]
    else:
        attempts = [grep_argv(req, mode, root, pattern, "-P"), grep_argv(req, mode, root, pattern, "-E")]

    for i, argv in enumerate(attempts):
        p, errf = run_search(argv)
        lines = []
        offset = int(req.get("offset") or 0)
        head_limit = req.get("head_limit")
        head_limit = int(head_limit) if head_limit else None
        killed = False
        if mode == "files_with_matches":
            files = [l.decode("utf-8", "replace").rstrip("\n") for l in p.stdout]
            p.wait()

            def mtime(f):
                try:
                    return os.stat(f).st_mtime
                except OSError:
                    return 0
            files.sort(key=mtime, reverse=True)
            lines = files[offset:]
            if head_limit is not None:
                lines = lines[:head_limit]
        else:
            skipped = 0
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if mode == "count" and not use_rg and line.endswith(":0"):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                lines.append(line)
                if head_limit is not None and len(lines) >= head_limit:
                    p.kill()
                    killed = True
                    break
            p.stdout.close()
            p.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace").strip()
        errf.close()
        code = 0 if killed else p.returncode
        if not use_rg and code == 2 and i == 0 and not lines and ("-P" in err or "support" in err):
            continue  # grep built without PCRE: retry with -E
        break

    col = Collector()
    total = 0
    if mode == "files_with_matches" and lines:
        col.add("Found %d file%s" % (len(lines), "" if len(lines) == 1 else "s"))
    for line in lines:
        if mode == "count":
            try:
                total += int(line.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                pass
        col.add(prefix + line if line.startswith("/") else line)
    if mode == "count" and lines:
        col.add("Found %d total occurrence%s across %d file%s." % (
            total, "" if total == 1 else "s", len(lines), "" if len(lines) == 1 else "s"))
    text = col.result(prefix)
    if not lines:
        text = "No files found" if mode == "files_with_matches" else "No matches found"
    if code >= 2 and err:
        text += "\n[%s reported errors: %s]" % (argv[0], err[:2000])
    reply({"ok": True, "text": text})


# ---------------------------------------------------------------- glob


def _glob_body(pat):
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 2)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "{":
            j = pat.find("}", i)
            if j < 0:
                out.append(re.escape(c))
            else:
                out.append("(?:" + "|".join(_glob_body(a) for a in pat[i + 1:j].split(",")) + ")")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def glob_regex(pattern):
    return re.compile("^" + _glob_body(pattern) + "$")


def op_glob(req):
    pattern = req["pattern"]
    root = expand(req.get("path") or "~")
    if pattern.startswith("/"):
        root, pattern = "/", pattern.lstrip("/")
    prefix = req.get("prefix") or ""
    limit = int(req.get("limit") or 100)
    if not os.path.isdir(root):
        raise UserError("%s is not a directory" % root)
    # Start the walk below the pattern's literal leading directories.
    parts = pattern.split("/")
    lead = []
    for part in parts[:-1]:
        if re.search(r"[*?\[{]", part):
            break
        lead.append(part)
    start = os.path.join(root, *lead) if lead else root
    max_depth = None if "**" in pattern else len(parts) - 1
    rx = glob_regex(pattern)
    found = []
    scanned = 0
    deadline = time.time() + 30
    partial = False
    for dirpath, dirnames, filenames in os.walk(start):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            rel = name if depth == 0 else rel_dir + "/" + name
            if rx.match(rel):
                full = os.path.join(dirpath, name)
                try:
                    found.append((os.lstat(full).st_mtime, full))
                except OSError:
                    pass
        scanned += len(filenames) + len(dirnames)
        if scanned > 500000 or time.time() > deadline:
            partial = True
            break
    found.sort(reverse=True)
    lines = [prefix + f for _, f in found[:limit]]
    text = "\n".join(lines) if lines else "No files found"
    if len(found) > limit:
        text += "\n(Results are truncated. Consider using a more specific path or pattern.)"
    if partial:
        text += "\n(Stopped scanning after %d entries; narrow the path.)" % scanned
    reply({"ok": True, "text": text})


# ---------------------------------------------------------------- run


def kill_group(pgid, grace=KILL_GRACE, child=None):
    """SIGTERM a process group, then SIGKILL whatever is left after `grace`.

    `child` is our own process in that group, if any: it has to be reaped, or
    its zombie keeps the group looking alive for the whole grace period."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if child is not None:
            child.poll()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def user_shell():
    return os.environ.get("SHELL") or "/bin/sh"


def command_env():
    """Environment for the user's command. The login shell sshd started has already
    sourced the rc files and exported the result to us, but its `exec` lowered
    SHLVL; restore it, or bash takes itself for the top-level ssh shell and sources
    ~/.bashrc all over again (seconds on a busy HPC login node)."""
    env = dict(os.environ)
    try:
        level = int(env.get("SHLVL") or 0)
    except ValueError:
        level = 0
    env["SHLVL"] = str(level + 1)
    return env


def op_run(req):
    """Run a command in its own process group; kill the whole group when the
    local side goes away (EOF on stdin) or we are signalled."""
    cwd = expand(req["cwd"]) if req.get("cwd") else None
    stop = []

    def on_signal(signum, frame):
        stop.append(signum)
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, on_signal)
    try:
        with open(os.devnull, "rb") as devnull:
            child = subprocess.Popen([user_shell(), "-c", req["cmd"]], stdin=devnull,
                                     cwd=cwd, env=command_env(), start_new_session=True)
    except OSError as e:
        sys.stderr.write("gekong run: cannot start command: %s\n" % e)
        return 127
    while child.poll() is None:
        if stop or stdin_closed(0.2):
            kill_group(child.pid, child=child)
            child.wait()
            return 128 + (stop[0] if stop else signal.SIGHUP)
    # Background leftovers would keep the channel open; take them down too.
    kill_group(child.pid, grace=1.0)
    rc = child.returncode
    return 128 - rc if rc < 0 else rc


# ---------------------------------------------------------------- jobs


SESSION = "gekong-"  # tmux session name prefix for jobs
ENV_SKIP = ("TMUX", "TMUX_PANE", "_", "PWD", "OLDPWD")


def jobs_root():
    d = os.path.join(scratch_dir(), "jobs")
    if not os.path.isdir(d):
        os.mkdir(d, 0o700)
    return d


def tmux(*args):
    p = subprocess.Popen(["tmux"] + list(args), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def job_meta(jid):
    with open(os.path.join(jobs_root(), jid, "meta.json")) as f:
        return json.load(f)


def resolve_job(ref):
    root = jobs_root()
    if re.match(r"^[a-z0-9]+$", ref) and os.path.isdir(os.path.join(root, ref)):
        return ref
    named = []
    for jid in os.listdir(root):
        try:
            m = job_meta(jid)
        except (OSError, ValueError):
            continue
        if m.get("name") == ref:
            named.append((m.get("started", 0), jid))
    if not named:
        raise UserError("no job %r on this host (see `gekong job ls`)" % ref)
    return max(named)[1]


def job_status(jid):
    d = os.path.join(jobs_root(), jid)
    try:
        with open(os.path.join(d, "exit")) as f:
            code = f.read().strip()
    except FileNotFoundError:
        code = None
    if code is not None:
        if code == "killed":
            return "killed", None
        return "exited", int(code) if code.lstrip("-").isdigit() else None
    rc, _, _ = tmux("has-session", "-t", SESSION + jid)
    return ("running", None) if rc == 0 else ("lost", None)


def op_job_start(req):
    if not shutil.which("tmux"):
        raise UserError("tmux is not installed on the remote host; gekong job needs it")
    root = jobs_root()
    jid = rand_id()
    d = os.path.join(root, jid)
    os.mkdir(d, 0o700)
    cwd = expand(req["cwd"]) if req.get("cwd") else os.path.expanduser("~")
    with open(os.path.join(d, "cmd"), "w") as f:
        f.write(req["cmd"])
    exports = ["export %s=%s" % (k, shquote(v)) for k, v in sorted(command_env().items())
               if k not in ENV_SKIP and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k)]
    q = lambda name: shquote(os.path.join(d, name))
    script = "\n".join([
        "#!/bin/sh",
        "\n".join(exports),
        "cd %s || { echo 'gekong job: cannot cd to %s' > %s; echo 1 > %s; exit 1; }"
        % (shquote(cwd), cwd.replace("'", ""), q("log"), q("exit")),
        '%s -c "$(cat %s)" < /dev/null > %s 2>&1' % (shquote(user_shell()), q("cmd"), q("log")),
        "echo $? > %s && mv %s %s" % (q("exit.tmp"), q("exit.tmp"), q("exit")),
        "",
    ])
    with open(os.path.join(d, "job.sh"), "w") as f:
        f.write(script)
    meta = {"id": jid, "name": req.get("name"), "cmd": req["cmd"], "cwd": cwd, "started": time.time()}
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(meta, f)
    rc, _, err = tmux("new-session", "-d", "-s", SESSION + jid, "sh " + q("job.sh"))
    if rc != 0:
        shutil.rmtree(d, ignore_errors=True)
        raise UserError("tmux new-session failed: %s" % err.strip())
    reply({"ok": True, "id": jid, "session": SESSION + jid, "log": os.path.join(d, "log")})


def op_job_ls(req):
    jobs = []
    root = jobs_root()
    for jid in os.listdir(root):
        try:
            m = job_meta(jid)
        except (OSError, ValueError):
            continue
        m["status"], m["code"] = job_status(jid)
        m["log"] = os.path.join(root, jid, "log")
        jobs.append(m)
    jobs.sort(key=lambda m: m.get("started", 0))
    reply({"ok": True, "jobs": jobs})


def op_job_kill(req):
    jid = resolve_job(req["job"])
    status, _ = job_status(jid)
    if status == "running":
        rc, out, _ = tmux("list-panes", "-t", SESSION + jid, "-F", "#{pane_pid}")
        for pid in out.split():
            kill_group(int(pid))
        tmux("kill-session", "-t", SESSION + jid)
        exit_path = os.path.join(jobs_root(), jid, "exit")
        if not os.path.exists(exit_path):
            with open(exit_path, "w") as f:
                f.write("killed\n")
    reply({"ok": True, "id": jid, "was": status})


def last_lines(path, n):
    """The last `n` lines of a file, and the file size they end at."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if n <= 0:
            return b"", end
        pos, data = end, b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        return b"\n".join(lines[-n:]) + (b"\n" if lines else b""), end


def job_exit_code(jid):
    status, code = job_status(jid)
    if status == "running":
        return None
    if status == "exited" and code is not None:
        return code
    return 143 if status == "killed" else 1


def op_job_tail(req):
    jid = resolve_job(req["job"])
    log = os.path.join(jobs_root(), jid, "log")
    while not os.path.exists(log):
        if job_exit_code(jid) is not None or stdin_closed(0.2):
            break
    if not os.path.exists(log):
        return 1
    data, pos = last_lines(log, int(req.get("lines") or 50))
    OUT.write(data)
    OUT.flush()
    if not req.get("follow"):
        return 0
    with open(log, "rb") as f:
        f.seek(pos)
        while True:
            chunk = f.read(65536)
            if chunk:
                OUT.write(chunk)
                OUT.flush()
                continue
            code = job_exit_code(jid)
            if code is not None:
                rest = f.read()
                if rest:
                    OUT.write(rest)
                    OUT.flush()
                return code
            if stdin_closed(0.5):
                return 130


def op_wait(req):
    """Block until a line matches `pattern` (exit 0, printing it), the timeout
    passes (1), or the job ends without a match (2). Without a pattern, wait for
    the job to end and exit with its code."""
    jid = resolve_job(req["job"]) if req.get("job") else None
    if jid:
        path = os.path.join(jobs_root(), jid, "log")
    elif req.get("file"):
        path = expand(req["file"])
    else:
        raise UserError("gekong wait needs --job or --file")
    rx = re.compile(req["pattern"]) if req.get("pattern") else None
    if rx is None and not jid:
        raise UserError("gekong wait --file needs --pattern")
    deadline = time.time() + float(req["timeout"]) if req.get("timeout") else None
    f = None
    pos = None
    partial = b""
    try:
        while True:
            if f is None and os.path.exists(path):
                f = open(path, "rb")
                if req.get("new") and pos is None:
                    f.seek(0, os.SEEK_END)
                pos = f.tell()
            got = False
            if f is not None and rx is not None:
                chunk = f.read(65536)
                if chunk:
                    got = True
                    data = partial + chunk
                    lines = data.split(b"\n")
                    partial = lines.pop()
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        if rx.search(line):
                            OUT.write(raw + b"\n")
                            OUT.flush()
                            return 0
            if got:
                continue
            if jid:
                code = job_exit_code(jid)
                if code is not None:
                    if rx is None:
                        return code
                    if f is not None and f.read(1):
                        f.seek(-1, os.SEEK_CUR)
                        continue
                    if partial and rx.search(partial.decode("utf-8", "replace")):
                        OUT.write(partial + b"\n")
                        OUT.flush()
                        return 0
                    sys.stderr.write("gekong wait: job %s ended (exit %s) without a match\n" % (jid, code))
                    return 2
            if deadline is not None and time.time() > deadline:
                sys.stderr.write("gekong wait: timed out after %ss\n" % req["timeout"])
                return 1
            if stdin_closed(0.5):
                return 130
    finally:
        if f is not None:
            f.close()


# ---------------------------------------------------------------- main


OPS = {
    "hello": op_hello,
    "read": op_read,
    "write": op_write,
    "grep": op_grep,
    "glob": op_glob,
    "run": op_run,
    "job_start": op_job_start,
    "job_ls": op_job_ls,
    "job_kill": op_job_kill,
    "job_tail": op_job_tail,
    "wait": op_wait,
}
STREAMING = ("run", "job_tail", "wait")


def main():
    req = json.loads(IN.readline().decode("utf-8"))
    op = req.get("op")
    streaming = op in STREAMING
    try:
        fn = OPS.get(op)
        if fn is None:
            raise UserError("unknown op %r (local and remote gekong out of sync?)" % op)
        rc = fn(req)
    except UserError as e:
        if streaming:
            sys.stderr.write("gekong: %s\n" % e)
            rc = 2
        else:
            reply({"ok": False, "kind": "error", "error": str(e)})
            rc = 0
    except (OSError, ValueError, KeyError) as e:
        if streaming:
            sys.stderr.write("gekong: %s: %s\n" % (type(e).__name__, e))
            rc = 2
        else:
            reply({"ok": False, "kind": "error", "error": "%s: %s" % (type(e).__name__, e)})
            rc = 0
    sys.stdout.flush()
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()

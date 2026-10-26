"""Unit tests: no ssh, no Docker. The helper runs locally through the real bootstrap."""
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from gekong import cli_gekong, cli_remote, hooks, mirror, paths, runs, setup_claude, ssh
from gekong.remote import helper


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GEKONG_HOME", str(tmp_path / "gk"))
    return tmp_path / "gk"


def test_mirror_path_mapping(home):
    mp = paths.mirror_path("gpu1", "/home/me/a.py")
    assert mp == home / "mirror" / "gpu1" / "home" / "me" / "a.py"
    assert paths.split_mirror_path(str(mp)) == ("gpu1", "/home/me/a.py")
    assert paths.split_mirror_path(str(home / "mirror" / "u@h" / "x" / ".." / "y")) == ("u@h", "/y")
    assert paths.split_mirror_path("/etc/hosts") is None
    assert paths.split_mirror_path(str(home / "mirrorx" / "h" / "a")) is None
    assert paths.is_under_mirror(str(home / "mirror"))
    for bad in ("", "-oProxyCommand=x", "a/b", "a b", ".."):
        with pytest.raises(ValueError):
            paths.check_host(bad)


def test_bare_gekong_prints_help(capsys):
    assert cli_gekong.main([]) == 0
    assert "gekong up HOST" in capsys.readouterr().out


def test_ssh_destination_parsing():
    assert cli_remote.ssh_destination(["host", "ls"]) == (0, [])
    assert cli_remote.ssh_destination(["-l", "me", "-p2222", "-t", "host", "ls", "-l"]) == (4, ["-l", "me", "-p", "2222"])
    assert cli_remote.ssh_destination(["-o", "SendEnv=GIT_PROTOCOL", "host", "git-upload-pack"]) == (
        2, ["-o", "SendEnv=GIT_PROTOCOL"])
    assert cli_remote.ssh_destination(["-tt", "-i", "key", "--", "host"]) == (4, [])
    assert cli_remote.ssh_destination(["-V"]) == (None, [])


def test_split_command():
    flags = {"-C": "cwd", "--name": "name"}
    assert cli_remote.split_command(["h", "ls", "-la"], flags) == ({}, "h", "ls -la")
    assert cli_remote.split_command(["-C", "/x", "h", "make"], flags) == ({"cwd": "/x"}, "h", "make")
    assert cli_remote.split_command(["h", "--name", "n", "-C", "/x", "run.sh"], flags) == (
        {"name": "n", "cwd": "/x"}, "h", "run.sh")
    assert cli_remote.split_command(["--", "h", "-C", "x"], flags) == ({}, "h", "-C x")
    for bad in ([], ["h"], ["-C"], ["-x", "h", "ls"]):
        with pytest.raises(cli_remote.Usage):
            cli_remote.split_command(bad, flags)


def test_glob_regex():
    rx = helper.glob_regex
    assert rx("*.py").match("a.py") and not rx("*.py").match("d/a.py")
    assert rx("**/*.py").match("a.py") and rx("**/*.py").match("d/e/a.py")
    assert rx("src/**").match("src/a/b") and not rx("src/**").match("lib/a")
    assert rx("*.{ts,tsx}").match("a.tsx") and not rx("*.{ts,tsx}").match("a.js")
    assert rx("[!a]?.c").match("bx.c") and not rx("[!a]?.c").match("ax.c")
    assert helper.expand_braces("*.{a,b}") == ["*.a", "*.b"]


def test_collector_keeps_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "scratch_dir", lambda: str(tmp_path))
    small = helper.Collector()
    for i in range(10):
        small.add(f"l{i}")
    assert small.result() == "\n".join(f"l{i}" for i in range(10))

    c = helper.Collector(limit=200, head=100, tail=60)
    for i in range(100):
        c.add(f"line {i:03d}")
    out = c.result(prefix="/mirror/h").splitlines()
    assert out[0] == "line 000" and out[-1] == "line 099"
    marker = next(l for l in out if "omitted" in l)
    spill = marker.split("saved at /mirror/h")[1].split(" - ")[0]
    assert len(open(spill).read().splitlines()) == 100
    kept = [l for l in out if "omitted" not in l]
    assert len("\n".join(kept)) <= 100 + 60


def test_settings_merge_is_idempotent_and_reversible():
    existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "/x/cc-status"}]}]},
                "permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus"}
    once, owned = setup_claude.merge(existing, "/bin/gekong-hook", "/m")
    twice, owned2 = setup_claude.merge(once, "/bin/gekong-hook", "/m", owned)
    assert (once, owned) == (twice, owned2)
    assert once["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/x/cc-status"
    assert len(once["hooks"]["PreToolUse"]) == 2 and len(once["hooks"]["PostToolUse"]) == 1
    assert once["permissions"]["allow"] == ["Bash(ls:*)", *setup_claude.ALLOW]
    assert once["permissions"]["additionalDirectories"] == ["/m"]
    assert setup_claude.unmerge(twice, owned2) == existing
    assert existing["permissions"]["allow"] == ["Bash(ls:*)"]  # input not mutated


def test_uninstall_keeps_permissions_the_user_already_had():
    existing = {"permissions": {"allow": ["Bash(gekong run:*)"], "ask": ["Bash(ssh:*)"]}}
    merged, owned = setup_claude.merge(existing, "/bin/gekong-hook", "/m")
    assert "Bash(gekong run:*)" not in owned["allow"] and "Bash(ssh:*)" not in owned["ask"]
    assert setup_claude.unmerge(merged, owned) == existing


def test_gekong_ssh_asks_first():
    # It forwards arbitrary ssh options, so pre-approving it would pre-approve port forwards.
    assert "Bash(gekong ssh:*)" in setup_claude.ASK and "Bash(gekong ssh:*)" not in setup_claude.ALLOW


def test_reinstall_moves_entries_an_older_install_added():
    old_allow = [*setup_claude.ALLOW, "Bash(gekong ssh:*)"]
    settings = {"permissions": {"allow": ["Bash(ls:*)", *old_allow]}}
    merged, owned = setup_claude.merge(settings, "/bin/gekong-hook", "/m", {"allow": old_allow})
    assert "Bash(gekong ssh:*)" not in merged["permissions"]["allow"]
    assert "Bash(gekong ssh:*)" in merged["permissions"]["ask"]
    assert setup_claude.unmerge(merged, owned) == {"permissions": {"allow": ["Bash(ls:*)"]}}


def test_skill_link_only_replaces_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    link = tmp_path / "claude" / "skills" / "gekong"
    assert setup_claude.link_skill().startswith("linked")
    assert link.resolve() == setup_claude.skill_source().resolve()
    assert setup_claude.link_skill().startswith("linked")  # re-install replaces our own link
    assert setup_claude.unlink_skill() and not link.is_symlink()

    mine = tmp_path / "my-skill"
    mine.mkdir()
    link.symlink_to(mine)
    assert "left it alone" in setup_claude.link_skill()
    assert setup_claude.unlink_skill() is None
    assert link.resolve() == mine.resolve()


def test_home_is_private(home):
    old = os.umask(0o022)
    try:
        mirror._atomic_write(paths.mirror_path("h", "/etc/secret"), b"s3cret")
    finally:
        os.umask(old)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    os.chmod(home, 0o755)  # created by an older version
    paths.ensure_private(paths.runs_dir())
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_pinned_base_survives_until_its_write_back(home):
    mirror.remember("toolu_B", "h", "/f", "sha-x")
    mirror.set_base("h", "/f", "sha-y")  # another session pushed meanwhile
    assert mirror.recall("toolu_B", "h", "/f") == "sha-x"
    assert mirror.recall("toolu_B", "h", "/f") is mirror.STORED  # used up
    mirror.remember("toolu_C", "h", "/f", None)
    assert mirror.recall("toolu_C", "h", "/other") is mirror.STORED


def test_run_identity_uses_start_time(home):
    run = runs.Run("h", ["gekong", "run", "h", "true"])
    meta = runs.find(run.id)
    assert meta["status"] == "running" and runs.same_process(meta)
    assert not runs.same_process({**meta, "pid_start": "Thu Jan  1 00:00:00 1970"})  # pid reused
    run.finish(0)
    assert runs.find(run.id)["status"] == "exited 0"


def test_last_lines(tmp_path):
    f = tmp_path / "log"
    f.write_bytes(b"a\nb\nc\n")
    assert helper.last_lines(str(f), 0) == (b"", 6)
    assert helper.last_lines(str(f), 2) == (b"b\nc\n", 6)
    assert helper.last_lines(str(f), 10) == (b"a\nb\nc\n", 6)


def test_hook_ignores_local_paths_and_redirects_search(home):
    assert hooks.handle({"hook_event_name": "PreToolUse", "tool_name": "Read",
                         "tool_input": {"file_path": "/etc/hosts"}}) is None
    assert hooks.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "ls"}}) is None
    out = hooks.handle({"hook_event_name": "PreToolUse", "tool_name": "Grep",
                        "tool_input": {"pattern": "x", "path": str(home / "mirror" / "gpu1" / "src")}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "host='gpu1', path='/src'" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_does_not_import_ssh_for_local_paths():
    code = ("import sys, json, io; from gekong import hooks;"
            "sys.stdin = io.StringIO(json.dumps({'hook_event_name':'PreToolUse','tool_name':'Read',"
            "'tool_input':{'file_path':'/etc/hosts'}})); hooks.main();"
            "print('gekong.ssh' in sys.modules, 'subprocess' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]


def call_local(req, data=None):
    """Run the helper through the real bootstrap with a local python3."""
    p = subprocess.run(["python3", "-c", ssh.BOOTSTRAP], input=ssh.payload(req, data), capture_output=True)
    assert p.returncode == 0, p.stderr
    head, _, body = p.stdout.partition(b"\n")
    return json.loads(head), body


def test_helper_read_write_cas(tmp_path):
    f = tmp_path / "d" / "f.txt"
    resp, _ = call_local({"op": "read", "path": str(f)})
    assert resp == {"ok": True, "exists": False}
    resp, _ = call_local({"op": "write", "path": str(f), "expect": "deadbeef"}, b"x")
    assert resp["kind"] == "conflict" and not f.exists()
    resp, _ = call_local({"op": "write", "path": str(f), "expect": None}, b"one\n")
    assert resp["ok"] and f.read_bytes() == b"one\n"
    sha = resp["sha"]
    resp, body = call_local({"op": "read", "path": str(f)})
    assert resp["sha"] == sha and body == b"one\n"
    resp, body = call_local({"op": "read", "path": str(f), "have": sha})
    assert "data_len" not in resp and body == b""
    resp, _ = call_local({"op": "write", "path": str(f), "expect": None}, b"two\n")
    assert resp["kind"] == "conflict" and resp["sha"] == sha
    resp, _ = call_local({"op": "read", "path": str(f), "max_bytes": 2})
    assert resp["too_large"]
    resp, _ = call_local({"op": "read", "path": "relative/path"})
    assert resp["kind"] == "error" and "absolute" in resp["error"]
    assert [p.name for p in f.parent.iterdir()] == ["f.txt"]  # no temp files left


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_helper_write_rechecks_before_rename(tmp_path):
    """A third-party write landing while the temp file is written must win."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"base\n")
    patch = ("import os;_fs=os.fsync;"
             f"os.fsync=lambda fd:(open({str(f)!r},'w').write('theirs\\n'),_fs(fd))[1];")
    p = subprocess.run(["python3", "-c", patch + ssh.BOOTSTRAP],
                       input=ssh.payload({"op": "write", "path": str(f), "expect": sha(b"base\n")}, b"ours\n"),
                       capture_output=True)
    resp = json.loads(p.stdout.splitlines()[0])
    assert resp["kind"] == "conflict" and resp["sha"] == sha(b"theirs\n")
    assert f.read_bytes() == b"theirs\n"
    assert [x.name for x in tmp_path.iterdir()] == ["f.txt"]


def test_helper_writes_are_serialized(tmp_path):
    """A gekong write waits for another gekong write to the same directory."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"base\n")
    lock = os.open(tmp_path, os.O_RDONLY)
    fcntl.flock(lock, fcntl.LOCK_EX)  # stands in for a concurrent gekong write
    p = subprocess.Popen(["python3", "-c", ssh.BOOTSTRAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.stdin.write(ssh.payload({"op": "write", "path": str(f), "expect": sha(b"base\n")}, b"ours\n"))
    p.stdin.close()
    time.sleep(0.5)
    assert p.poll() is None  # blocked on the lock
    f.write_bytes(b"theirs\n")  # the other write completes...
    os.close(lock)  # ...and releases the lock
    resp = json.loads(p.stdout.read().splitlines()[0])
    p.wait(timeout=10)
    assert resp["kind"] == "conflict" and f.read_bytes() == b"theirs\n"

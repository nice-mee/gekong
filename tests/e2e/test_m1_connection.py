"""M1: one login, many reuses; never a login outside `gekong up`."""
import subprocess

from conftest import BIN, HOST


def test_up_status_down(env):
    r = env.run("gekong", "up", HOST)
    assert r.returncode == 0, r.stderr
    assert "connected to gekong-test" in r.stdout and "python 3" in r.stdout

    r = env.run("gekong", "status")
    assert "connected" in r.stdout and "ms per call" in r.stdout

    r = env.run("gk", "ssh", HOST, "echo", "hi from", "$HOSTNAME")
    assert r.returncode == 0 and r.stdout.startswith("hi from"), r.stderr

    r = env.run("gekong", "down", HOST)
    assert "disconnected" in r.stdout
    r = env.run("gekong", "status", HOST)
    assert r.returncode == 1 and "down" in r.stdout


def test_up_is_idempotent(up):
    r = up.run("gekong", "up", HOST)
    assert r.returncode == 0 and "already connected" in r.stdout


def test_never_logs_in_without_master(env):
    # The key would let ssh log in directly; gekong must refuse instead.
    for argv in (["gk", "ssh", HOST, "true"], ["gk", "run", HOST, "true"]):
        r = env.run(*argv)
        assert r.returncode == 255, (argv, r.stdout, r.stderr)
        assert f"gekong up {HOST}" in r.stderr


def test_gk_ssh_with_rsync_and_user_flag(up, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha\n")
    r = subprocess.run(["rsync", "-a", "-e", f"{BIN / 'gk'} ssh", f"{src}/", f"{HOST}:w/synced/"],
                       capture_output=True, text=True, env=up.env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert up.remote("cat ~/w/synced/a.txt") == "alpha\n"
    # `-l user host` lands on the same master as `host` (ControlPath uses %C).
    r = up.run("gk", "ssh", "-l", "tester", HOST, "whoami")
    assert r.stdout.strip() == "tester", r.stderr


def test_client_ssh_fails_closed_when_master_vanishes(env, monkeypatch):
    """Even past the up-front check (a race), a client ssh must not log in by itself."""
    from gekong import ssh
    monkeypatch.setenv("GEKONG_HOME", str(env.home))
    monkeypatch.setenv("GEKONG_SSH_CONFIG", str(env.config))
    for argv in (ssh.helper_argv(HOST, "hello"), ["ssh", *ssh.client_opts(HOST), HOST, "echo", "logged-in"]):
        r = subprocess.run(argv, input=ssh.payload({"op": "hello"}), capture_output=True, timeout=30)
        assert r.returncode == 255 and b"logged-in" not in r.stdout and b"python" not in r.stdout, r

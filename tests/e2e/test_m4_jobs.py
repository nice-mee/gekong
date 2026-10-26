"""M4: long jobs in remote tmux, decoupled from the connection; gekong wait."""
import subprocess
import time

from conftest import HOST


def start(up, *argv):
    r = up.run("gekong", "job", "start", *argv)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[2]  # "started job <id> on ..."


def test_job_lifecycle(up):
    jid = start(up, "-C", "/tmp", "--name", "count", HOST, "pwd; for i in 1 2 3; do echo step $i; sleep 1; done; exit 7")
    ls = up.run("gekong", "job", "ls", HOST).stdout
    assert jid in ls and "count" in ls and "running" in ls

    r = up.run("gekong", "wait", HOST, "--job", "count", "--pattern", r"step \d", timeout=30)
    assert (r.returncode, r.stdout) == (0, "step 1\n")
    r = up.run("gekong", "wait", HOST, "--job", jid, timeout=30)
    assert r.returncode == 7
    r = up.run("gekong", "job", "tail", HOST, jid, "-n", "2")
    assert r.stdout == "step 2\nstep 3\n"
    r = up.run("gekong", "job", "tail", HOST, jid, "-f")
    assert r.returncode == 7 and r.stdout.startswith("/tmp\nstep 1")
    assert "exited 7" in up.run("gekong", "job", "ls", HOST).stdout


def test_wait_timeout_and_no_match(up):
    jid = start(up, HOST, "echo only line; sleep 2")
    r = up.run("gekong", "wait", HOST, "--job", jid, "--pattern", "never", "--timeout", "0.5")
    assert r.returncode == 1 and "timed out" in r.stderr
    r = up.run("gekong", "wait", HOST, "--job", jid, "--pattern", "never", timeout=30)
    assert r.returncode == 2 and "without a match" in r.stderr
    up.remote("echo 'ready on port 8080' >> ~/w/server.log")
    r = up.run("gekong", "wait", HOST, "--file", "~/w/server.log", "--pattern", "port (\\d+)")
    assert r.stdout == "ready on port 8080\n"


def test_job_survives_disconnect_and_kill_stops_group(up):
    jid = start(up, HOST, "sleep 4810 & sleep 4811")
    up.run("gekong", "down", HOST)
    time.sleep(1)
    assert len(up.remote("pgrep -f '^sleep 481[01]$' || true").split()) == 2
    assert up.run("gekong", "up", HOST).returncode == 0
    assert "running" in up.run("gekong", "job", "ls", HOST).stdout
    r = up.run("gekong", "job", "kill", HOST, jid)
    assert "killed" in r.stdout
    assert up.remote("pgrep -f '^sleep 481[01]$' || true").strip() == ""
    assert "killed" in up.run("gekong", "job", "ls", HOST).stdout
    assert up.remote(f"tmux has-session -t gekong-{jid} 2>&1 || true").strip() != ""


def test_follow_streams_and_stops_with_local_kill(up):
    jid = start(up, HOST, "for i in $(seq 1 100); do echo tick $i; sleep 0.2; done")
    p = up.popen("gekong", "job", "tail", HOST, jid, "-f", stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().startswith("tick")
    p.terminate()
    p.wait(timeout=10)
    time.sleep(0.5)
    # the remote tail went away with it; the job itself keeps going
    assert up.remote("pgrep -f '^python3 -c import sys' || true").strip() == ""
    assert "running" in up.run("gekong", "job", "ls", HOST).stdout
    up.run("gekong", "job", "kill", HOST, jid)

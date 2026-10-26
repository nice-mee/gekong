"""M3: gk run is an ordinary local process whose death takes the remote process group with it."""
import os
import signal
import subprocess
import time

import pytest

from conftest import BIN, HOST


def remote_procs(up, pattern):
    # anchored, so neither the shell running pgrep nor the `sh -c` parent matches
    return up.remote(f"pgrep -f '^{pattern}$' || true").split()


def wait_until(pred, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_streams_separate_and_exit_code(up):
    r = up.run("gk", "run", HOST, "echo out; echo err >&2; exit 3")
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")

    r = up.run("gk", "run", "-C", "/etc", HOST, "pwd")
    assert r.stdout == "/etc\n"
    r = up.run("gk", "run", HOST, "echo", "several", "words")
    assert r.stdout == "several words\n"


def test_output_streams_before_exit(up):
    p = up.popen("gk", "run", HOST, "echo first; sleep 3; echo second", stdout=subprocess.PIPE, text=True)
    t0 = time.time()
    assert p.stdout.readline() == "first\n"
    assert time.time() - t0 < 2.5
    assert p.stdout.readline() == "second\n"
    assert p.wait(timeout=10) == 0


def test_leftover_background_processes_do_not_hang(up):
    t0 = time.time()
    r = up.run("gk", "run", HOST, "sleep 471 & echo started", timeout=20)
    assert r.stdout == "started\n" and time.time() - t0 < 5
    assert wait_until(lambda: not remote_procs(up, "sleep 471"))


def test_closed_stdout_stops_remote(up):
    gk = subprocess.list2cmdline([str(BIN / "gk"), "run", HOST, "yes"])
    r = subprocess.run(f"{gk} | head -1", shell=True, capture_output=True, text=True, env=up.env, timeout=30)
    assert r.stdout == "y\n" and "Traceback" not in r.stderr
    assert wait_until(lambda: not remote_procs(up, "yes"))


@pytest.mark.parametrize("how", ["sigterm", "sigint", "sigkill", "tree"])
def test_killing_gk_kills_remote_group(up, how):
    marker = {"sigterm": 472, "sigint": 473, "sigkill": 474, "tree": 475}[how]
    p = up.popen("gk", "run", HOST, f"sleep {marker}0 & sleep {marker}1; echo never",
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert wait_until(lambda: len(remote_procs(up, f"sleep {marker}[01]")) == 2)
    if how == "tree":  # what an agent's kill of a background command does
        ssh_pids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        for pid in [p.pid, *map(int, ssh_pids)]:
            os.kill(pid, signal.SIGTERM)
    else:
        p.send_signal({"sigterm": signal.SIGTERM, "sigint": signal.SIGINT, "sigkill": signal.SIGKILL}[how])
    p.wait(timeout=20)
    assert wait_until(lambda: not remote_procs(up, f"sleep {marker}[01]")), how
    assert b"never" not in p.stdout.read()


def test_ps_tail_kill(up):
    p = up.popen("gk", "run", HOST, "echo hello-ps; sleep 4760", stdout=subprocess.DEVNULL)
    assert wait_until(lambda: "sleep 4760" in up.run("gekong", "ps").stdout)
    run_id = next(l.split()[0] for l in up.run("gekong", "ps").stdout.splitlines() if "4760" in l)
    assert wait_until(lambda: "hello-ps" in up.run("gekong", "tail", run_id).stdout)
    r = up.run("gekong", "kill", run_id)
    assert "stopped" in r.stdout, r.stderr
    assert p.wait(timeout=10) != 0
    assert wait_until(lambda: not remote_procs(up, "sleep 4760"))
    assert "sleep 4760" not in up.run("gekong", "ps").stdout
    assert "exited" in up.run("gekong", "ps", "-a").stdout


def test_rc_files_sourced_once_and_skippable_for_file_calls(up):
    """sshd's shell sources ~/.bashrc; the command's own shell must not do it again.
    File calls are tagged gekong:call so a slow rc file can skip itself for them."""
    rc = "/home/tester/.bashrc"
    up.root(f"cp {rc} {rc}.orig")
    try:
        up.root(f"""printf '%s\\n' '[[ $BASH_EXECUTION_STRING == *gekong:call* ]] && return' \
                    'echo sourced >> ~/w/rc-count' > {rc} && rm -f /home/tester/w/rc-count""")
        count = lambda: len(up.root("cat /home/tester/w/rc-count 2>/dev/null || true").split())
        r = up.run("gk", "run", HOST, "echo $SHLVL")
        assert r.returncode == 0 and count() == 1, r.stderr
        up.root("echo hi > /home/tester/w/f.txt && chown tester /home/tester/w/f.txt")
        mp = up.mirror("/home/tester/w/f.txt")
        assert up.hook("PreToolUse", "Read", {"file_path": str(mp)}) is None and mp.read_text() == "hi\n"
        assert count() == 1  # skipped for the file call
    finally:
        up.root(f"mv {rc}.orig {rc}")

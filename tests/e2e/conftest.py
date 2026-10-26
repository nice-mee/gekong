"""End-to-end fixtures: a throwaway sshd container standing in for the remote host.

The container listens on 127.0.0.1:$GEKONG_TEST_PORT (default 2222) and accepts a
key generated per session. Tests talk to it as host "gekong-test" through a
private ssh config (GEKONG_SSH_CONFIG), with GEKONG_HOME in a short /tmp path so
ControlPath sockets stay under the macOS length limit.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

IMAGE = "gekong-test-sshd"
CONTAINER = "gekong-test"
HOST = "gekong-test"
PORT = int(os.environ.get("GEKONG_TEST_PORT", "2222"))
HERE = Path(__file__).parent
BIN = Path(sys.executable).parent


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kw)


@pytest.fixture(scope="session")
def sshd():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("Docker is not available")
    _docker("build", "-q", "-t", IMAGE, str(HERE))
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    _docker("run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:22", IMAGE)
    work = Path(tempfile.mkdtemp(prefix="gk-", dir="/tmp"))
    key = work / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    _docker("exec", "-i", CONTAINER, "sh", "-c",
            "mkdir -p /home/tester/.ssh && cat > /home/tester/.ssh/authorized_keys"
            " && chown -R tester:tester /home/tester/.ssh && chmod 700 /home/tester/.ssh"
            " && chmod 600 /home/tester/.ssh/authorized_keys",
            input=(work / "id.pub").read_text())
    config = work / "ssh_config"
    config.write_text(f"""Host {HOST}
  HostName 127.0.0.1
  Port {PORT}
  User tester
  IdentityFile {key}
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
""")
    for _ in range(50):
        if subprocess.run(["ssh", "-F", str(config), "-o", "BatchMode=yes", HOST, "true"],
                          capture_output=True).returncode == 0:
            break
        time.sleep(0.2)
    else:
        pytest.fail("sshd container did not come up")
    yield {"work": work, "config": config}
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    shutil.rmtree(work, ignore_errors=True)


class Env:
    """One GEKONG_HOME plus helpers to run gekong commands and inspect the remote."""

    def __init__(self, sshd):
        self.home = Path(tempfile.mkdtemp(prefix="h", dir=sshd["work"]))
        self.config = sshd["config"]
        self.env = {**os.environ, "GEKONG_HOME": str(self.home), "GEKONG_SSH_CONFIG": str(self.config)}

    def run(self, *argv, input=None, timeout=60, check=False):
        return subprocess.run([str(BIN / argv[0]), *argv[1:]], input=input, capture_output=True, text=True,
                              env=self.env, timeout=timeout, check=check)

    def popen(self, *argv, **kw):
        return subprocess.Popen([str(BIN / argv[0]), *argv[1:]], env=self.env, **kw)

    def remote(self, cmd, input=None, check=True):
        """Run a command on the container directly, independent of gekong."""
        r = subprocess.run(["ssh", "-F", str(self.config), "-o", "BatchMode=yes", HOST, cmd],
                           input=input, capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise AssertionError(f"remote {cmd!r} failed: {r.stderr}")
        return r.stdout

    def root(self, cmd):
        return _docker("exec", CONTAINER, "sh", "-c", cmd).stdout

    def mirror(self, remote_path):
        return self.home / "mirror" / HOST / remote_path.lstrip("/")

    def hook(self, event, tool, tool_input, cwd="/tmp", tool_use_id=None):
        import json
        ev = {"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        if tool_use_id:
            ev["tool_use_id"] = tool_use_id
        r = self.run("gekong-hook", input=json.dumps(ev))
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout) if r.stdout.strip() else None


@pytest.fixture
def env(sshd):
    e = Env(sshd)
    yield e
    e.run("gekong", "down", "--all")


@pytest.fixture
def up(env):
    r = env.run("gekong", "up", HOST)
    assert r.returncode == 0, r.stdout + r.stderr
    env.remote("rm -rf ~/w && mkdir -p ~/w")
    return env

"""The remote helper on the oldest supported python (3.6), straight through the bootstrap."""
import json
import shutil
import subprocess

import pytest

from gekong import ssh

IMAGE = "python:3.6-slim"


@pytest.fixture(scope="module")
def py36():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("Docker is not available")
    if subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode:
        pytest.skip(f"cannot pull {IMAGE}")
    name = "gekong-py36"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, IMAGE, "sleep", "600"], check=True, capture_output=True)
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def call(name, req, data=None, streaming=False):
    argv = ["docker", "exec", "-i", name, "sh", "-c", ssh.remote_cmd(req["op"])]
    if streaming:  # keep stdin open like gk does: EOF means "local side gone"
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(ssh.payload(req))
        p.stdin.flush()
        out, err = p.stdout.read(), p.stderr.read()
        p.wait(timeout=60)
        p.stdin.close()
        return subprocess.CompletedProcess(argv, p.returncode, out, err)
    p = subprocess.run(argv, input=ssh.payload(req, data), capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    head, _, body = p.stdout.partition(b"\n")
    return json.loads(head), body


def test_ops_on_python36(py36):
    resp, _ = call(py36, {"op": "hello"})
    assert resp["python"].startswith("3.6") and resp["system"] == "Linux"
    resp, _ = call(py36, {"op": "write", "path": "/tmp/t/a.py", "expect": None}, b"def foo():\n    pass\n")
    assert resp["ok"]
    resp, body = call(py36, {"op": "read", "path": "/tmp/t/a.py"})
    assert body == b"def foo():\n    pass\n"
    resp, _ = call(py36, {"op": "glob", "path": "/tmp/t", "pattern": "**/*.{py,txt}", "prefix": "/m"})
    assert resp["text"] == "/m/tmp/t/a.py"
    resp, _ = call(py36, {"op": "grep", "path": "/tmp/t", "pattern": "foo", "output_mode": "content", "prefix": "/m"})
    assert resp["text"] == "/m/tmp/t/a.py:1:def foo():"  # GNU grep fallback: no rg in this image
    p = call(py36, {"op": "run", "cmd": "echo out; echo err >&2; exit 4"}, streaming=True)
    assert (p.returncode, p.stdout, p.stderr) == (4, b"out\n", b"err\n")

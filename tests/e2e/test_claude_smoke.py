"""Real Claude Code against the sshd container (opt-in: GEKONG_CLAUDE_SMOKE=1; spends tokens).

Wires gekong into a headless `claude -p` exactly as `gekong setup claude` would
(hooks, mirror as an extra directory, gekong MCP server) and checks that:
- a native Edit on a mirror path lands on the remote;
- when the remote file changes between the model's Read and its Edit, the edit is
  refused, the model re-reads, and the final file has both changes.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import BIN, HOST
from gekong import setup_claude

pytestmark = pytest.mark.skipif(os.environ.get("GEKONG_CLAUDE_SMOKE") != "1",
                                reason="set GEKONG_CLAUDE_SMOKE=1 to run Claude Code for real")

CONCURRENT = """
import json, os, subprocess, sys
event = json.load(sys.stdin)
path = (event.get("tool_input") or {}).get("file_path", "")
marker = sys.argv[2]
if path.endswith("/w/race.py") and not os.path.exists(marker):
    open(marker, "w").close()
    subprocess.run(["ssh", "-F", sys.argv[1], "gekong-test",
                    "sed -i 's/^VERSION = 1$/VERSION = 2/' ~/w/race.py"], check=True)
"""


def claude(up, tmp_path, prompt):
    mirror = str(up.home / "mirror")
    settings, _ = setup_claude.merge({}, str(BIN / "gekong-hook"), mirror)
    script = tmp_path / "concurrent.py"
    script.write_text(CONCURRENT)
    settings["hooks"].setdefault("PostToolUse", []).append({"matcher": "Read", "hooks": [
        {"type": "command", "command": f"{sys.executable} {script} {up.config} {tmp_path / 'raced'}"}]})
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"gekong": {"command": str(BIN / "gekong-mcp")}}}))
    argv = ["claude", "-p", prompt, "--model", os.environ.get("GEKONG_SMOKE_MODEL", "sonnet"),
            "--setting-sources", "project", "--settings", str(tmp_path / "settings.json"),
            "--mcp-config", str(tmp_path / "mcp.json"), "--strict-mcp-config", "--add-dir", mirror,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Edit", "Write", *setup_claude.ALLOW,
            "--output-format", "stream-json", "--verbose"]
    r = subprocess.run(argv, capture_output=True, text=True, env=up.env, cwd=tmp_path, timeout=600)
    events = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    results = [c for e in events if e.get("type") == "user"
               for c in (e["message"].get("content") or []) if isinstance(c, dict) and c.get("type") == "tool_result"]
    calls = [c for e in events if e.get("type") == "assistant"
             for c in e["message"]["content"] if c.get("type") == "tool_use"]
    return r, calls, results


def text_of(result):
    c = result.get("content")
    return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [])


def test_native_edit_reaches_remote_and_races_are_caught(up, tmp_path):
    home = up.remote("echo $HOME").strip()
    up.remote("printf 'def greet():\\n    return \"hello\"\\n' > ~/w/app.py")
    up.remote("printf 'VERSION = 1\\nNAME = \"old\"\\n' > ~/w/race.py")
    mirror = up.home / "mirror" / HOST
    prompt = (f"Remote host {HOST} is mounted through gekong. Use the gekong MCP Grep tool (host {HOST}) to find "
              f"which file under {home}/w defines greet. Then, with your normal Read/Edit tools on the mirror "
              f"paths under {mirror}, change greet's return value to \"bonjour\" in that file, and in "
              f"{mirror}{home}/w/race.py change NAME to \"new\". If an edit is refused, follow the instructions "
              "in the error. Reply DONE when finished.")
    r, calls, results = claude(up, tmp_path, prompt)
    assert r.returncode == 0, r.stderr[-2000:]
    assert any(c["name"] == "mcp__gekong__Grep" for c in calls)
    assert up.remote("cat ~/w/app.py") == 'def greet():\n    return "bonjour"\n'
    # the concurrent VERSION bump survived and the model's edit was applied on top of it
    assert up.remote("cat ~/w/race.py") == 'VERSION = 2\nNAME = "new"\n'
    refused = [t for t in map(text_of, results) if "since you last read it" in t]
    print("\n--- race handling:", refused[:1] or "no refusal seen (Claude Code's own check fired first?)")
    print("--- tool calls:", [(c["name"], c["input"].get("file_path", "")[-12:]) for c in calls])

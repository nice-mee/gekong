"""M2: native file tools on the mirror (hooks) and remote search (MCP)."""
import asyncio
import os
import time

import pytest

from conftest import BIN, HOST

PRE, POST = "PreToolUse", "PostToolUse"


def path_of(up, name):
    home = up.remote("echo $HOME").strip()
    return f"{home}/w/{name}"


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None


def test_read_pulls_and_keeps_mtime_when_unchanged(up):
    rp = path_of(up, "a.py")
    up.remote(f"printf 'x = 1\\n' > {rp}")
    mp = up.mirror(rp)
    assert up.hook(PRE, "Read", {"file_path": str(mp)}) is None
    assert mp.read_text() == "x = 1\n"
    t = mp.stat().st_mtime_ns
    time.sleep(0.05)
    assert up.hook(PRE, "Read", {"file_path": str(mp)}) is None
    assert mp.stat().st_mtime_ns == t  # unchanged content: file untouched

    up.remote(f"printf 'x = 2\\n' > {rp}")
    up.hook(PRE, "Read", {"file_path": str(mp)})
    assert mp.read_text() == "x = 2\n"


def test_read_missing_and_directory(up):
    rp = path_of(up, "gone.txt")
    up.remote(f"echo hi > {rp}")
    mp = up.mirror(rp)
    up.hook(PRE, "Read", {"file_path": str(mp)})
    assert mp.exists()
    up.remote(f"rm {rp}")
    assert up.hook(PRE, "Read", {"file_path": str(mp)}) is None
    assert not mp.exists()  # Read then reports "does not exist" by itself

    out = up.hook(PRE, "Read", {"file_path": str(up.mirror(path_of(up, "")))})
    assert out is None or "directory" in reason(out)
    up.remote("mkdir -p ~/w/sub")
    out = up.hook(PRE, "Read", {"file_path": str(up.mirror(path_of(up, "sub")))})
    assert "is a directory" in reason(out)


def test_edit_round_trip_preserves_mode_and_symlink(up):
    rp = path_of(up, "run.sh")
    up.remote(f"printf '#!/bin/sh\\necho old\\n' > {rp} && chmod 755 {rp} && ln -s run.sh ~/w/link.sh")
    mp = up.mirror(rp)
    up.hook(PRE, "Read", {"file_path": str(mp)})
    assert up.hook(PRE, "Edit", {"file_path": str(mp), "old_string": "old", "new_string": "new"}) is None
    mp.write_text(mp.read_text().replace("old", "new"))  # what the native Edit does
    assert up.hook(POST, "Edit", {"file_path": str(mp)}) is None
    assert up.remote(f"cat {rp}") == "#!/bin/sh\necho new\n"
    assert up.remote(f"stat -c %a {rp}").strip() == "755"

    lp = path_of(up, "link.sh")
    ml = up.mirror(lp)
    up.hook(PRE, "Read", {"file_path": str(ml)})
    ml.write_text("#!/bin/sh\necho via link\n")
    assert up.hook(POST, "Write", {"file_path": str(ml)}) is None
    assert up.remote("readlink ~/w/link.sh").strip() == "run.sh"
    assert up.remote(f"cat {rp}") == "#!/bin/sh\necho via link\n"


def test_edit_refused_when_remote_changed(up):
    rp = path_of(up, "b.txt")
    up.remote(f"echo one > {rp}")
    mp = up.mirror(rp)
    up.hook(PRE, "Read", {"file_path": str(mp)})
    up.remote(f"echo two > {rp}")
    out = up.hook(PRE, "Edit", {"file_path": str(mp), "old_string": "one", "new_string": "uno"})
    assert "changed on gekong-test since you last read it" in reason(out)
    assert mp.read_text() == "two\n"  # refreshed for the re-read
    assert up.hook(PRE, "Edit", {"file_path": str(mp)}) is None  # now in sync


def test_push_conflict_keeps_remote_change(up):
    rp = path_of(up, "c.txt")
    up.remote(f"echo base > {rp}")
    mp = up.mirror(rp)
    up.hook(PRE, "Read", {"file_path": str(mp)})
    assert up.hook(PRE, "Edit", {"file_path": str(mp)}) is None
    up.remote(f"echo theirs > {rp}")  # lands while the edit waits for approval
    mp.write_text("ours\n")
    out = up.hook(POST, "Edit", {"file_path": str(mp)})
    assert out["decision"] == "block" and "NOT written" in out["reason"]
    assert up.remote(f"cat {rp}") == "theirs\n"
    assert mp.read_text() == "theirs\n"


def test_write_new_file_and_refuse_blind_overwrite(up):
    rp = path_of(up, "new/deep/n.txt")
    mp = up.mirror(rp)
    assert up.hook(PRE, "Write", {"file_path": str(mp), "content": "fresh\n"}) is None
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("fresh\n")
    assert up.hook(POST, "Write", {"file_path": str(mp)}) is None
    assert up.remote(f"cat {rp}") == "fresh\n"

    rp2 = path_of(up, "exists.txt")
    up.remote(f"echo keep > {rp2}")
    out = up.hook(PRE, "Write", {"file_path": str(up.mirror(rp2)), "content": "clobber"})
    assert "already exists" in reason(out)
    assert up.mirror(rp2).read_text() == "keep\n"


def test_disconnected_and_fast_paths(up):
    assert up.hook(PRE, "Read", {"file_path": "/etc/hosts"}) is None
    assert up.hook(PRE, "Grep", {"pattern": "x", "path": "/tmp"}) is None
    out = up.hook(PRE, "Grep", {"pattern": "x", "path": str(up.mirror(path_of(up, "")))})
    assert "gekong MCP Grep" in reason(out)
    out = up.hook(PRE, "Glob", {"pattern": "*"}, cwd=str(up.mirror("/home")))
    assert "gekong MCP Glob" in reason(out)

    up.run("gekong", "down", HOST)
    out = up.hook(PRE, "Read", {"file_path": str(up.mirror(path_of(up, "a.py")))})
    assert f"gekong up {HOST}" in reason(out)
    mp = up.mirror(path_of(up, "lost.txt"))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("edit\n")
    out = up.hook(POST, "Write", {"file_path": str(mp)})
    assert "NOT written" in out["reason"] and not mp.exists()


# ---------------------------------------------------------------- MCP search


@pytest.fixture
def tree(up):
    up.remote("mkdir -p ~/w/proj/src/pkg ~/w/proj/.git && cd ~/w/proj"
              " && printf 'def foo():\\n    return 1\\n' > src/pkg/a.py"
              " && printf 'import os\\nfoo()\\n' > src/main.py"
              " && printf 'const foo = 1\\n' > src/x.ts"
              " && echo 'foo in git' > .git/config")
    return up


def tool(up, name, **args):
    os.environ.update(GEKONG_HOME=str(up.home), GEKONG_SSH_CONFIG=str(up.config))
    from gekong import mcp_server
    return mcp_server.run_tool(name, {"host": HOST, **args})


def test_grep_modes_and_mirror_paths(tree):
    root = path_of(tree, "proj")
    prefix = str(tree.mirror(""))
    out = tool(tree, "Grep", pattern="foo", path=root)
    assert out.splitlines()[0] == "Found 3 files"
    assert all(l.startswith(prefix) for l in out.splitlines()[1:])
    assert ".git" not in out

    out = tool(tree, "Grep", pattern="foo", path=root, output_mode="content", glob="*.py", **{"-A": 1})
    assert f"{tree.mirror(root)}/src/pkg/a.py:1:def foo():" in out
    assert f"{tree.mirror(root)}/src/pkg/a.py-2-    return 1" in out
    assert "x.ts" not in out

    out = tool(tree, "Grep", pattern="FOO", path=str(tree.mirror(root)), output_mode="count", **{"-i": True})
    assert "Found 3 total occurrences across 3 files." in out

    assert tool(tree, "Grep", pattern="nomatch_xyz", path=root) == "No files found"


def test_grep_without_ripgrep(tree):
    tree.root("mv /usr/bin/rg /usr/bin/rg.off")
    try:
        root = path_of(tree, "proj")
        out = tool(tree, "Grep", pattern=r"foo\(\)", path=root, output_mode="content", glob="*.{py,ts}")
        assert f"{tree.mirror(root)}/src/main.py:2:foo()" in out and ".git" not in out
        with pytest.raises(Exception, match="needs ripgrep"):
            tool(tree, "Grep", pattern="foo", path=root, type="py")
    finally:
        tree.root("mv /usr/bin/rg.off /usr/bin/rg")


def test_grep_truncates_and_spills(up):
    up.remote("mkdir -p ~/w/big && python3 -c \"import sys; [print('line %05d match' % i) for i in range(5000)]\""
              " > ~/w/big/log.txt")
    out = tool(up, "Grep", pattern="match", path=path_of(up, "big"), output_mode="content")
    assert len(out) < 21000
    assert "line 00000 match" in out and "line 04999 match" in out
    marker = next(l for l in out.splitlines() if "lines omitted" in l)
    spill = marker.split("saved at ")[1].split(" - ")[0]
    assert spill.startswith(str(up.mirror("/tmp/gekong-")))
    # The spill file is readable through the mirror and searchable with Grep.
    assert up.hook(PRE, "Read", {"file_path": spill}) is None
    assert len(open(spill).read().splitlines()) == 5000
    out = tool(up, "Grep", pattern="line 02500", path=spill, output_mode="content")
    assert out.endswith(":2501:line 02500 match")


def test_glob(tree):
    root = path_of(tree, "proj")
    out = tool(tree, "Glob", pattern="**/*.py", path=root)
    assert sorted(out.splitlines()) == sorted(str(tree.mirror(f"{root}/{p}")) for p in ("src/pkg/a.py", "src/main.py"))
    assert tool(tree, "Glob", pattern="*.py", path=root) == "No files found"
    out = tool(tree, "Glob", pattern="src/*.{py,ts}", path=str(tree.mirror(root)))
    assert len(out.splitlines()) == 2
    tree.remote("sleep 1; touch ~/w/proj/src/pkg/a.py")
    assert tool(tree, "Glob", pattern="**/*.py", path=root).splitlines()[0].endswith("a.py")


def test_mcp_protocol(tree):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=str(BIN / "gekong-mcp"), env=tree.env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                assert "mirror" in (init.instructions or "")
                tools = await s.list_tools()
                assert {t.name for t in tools.tools} == {"Grep", "Glob"}
                res = await s.call_tool("Glob", {"host": HOST, "pattern": "**/*.ts", "path": path_of(tree, "proj")})
                assert not res.is_error and res.content[0].text.endswith("src/x.ts")
                res = await s.call_tool("Grep", {"host": "nope-host", "pattern": "x"})
                assert res.is_error and "gekong up nope-host" in res.content[0].text

    asyncio.run(go())


def test_two_sessions_cannot_clobber_each_other(up):
    """B's Write passes its check, A edits and pushes, then B writes back stale
    full-file content: B must be refused, A's change kept."""
    rp = path_of(up, "shared.txt")
    up.remote(f"echo v0 > {rp}")
    mp = up.mirror(rp)
    up.hook(PRE, "Read", {"file_path": str(mp)})
    assert up.hook(PRE, "Write", {"file_path": str(mp)}, tool_use_id="toolu_B") is None
    assert up.hook(PRE, "Edit", {"file_path": str(mp)}, tool_use_id="toolu_A") is None
    mp.write_text("from A\n")
    assert up.hook(POST, "Edit", {"file_path": str(mp)}, tool_use_id="toolu_A") is None
    assert up.remote(f"cat {rp}") == "from A\n"
    mp.write_text("from B\n")
    out = up.hook(POST, "Write", {"file_path": str(mp)}, tool_use_id="toolu_B")
    assert out["decision"] == "block" and "NOT written" in out["reason"]
    assert up.remote(f"cat {rp}") == "from A\n"
    assert mp.read_text() == "from A\n"

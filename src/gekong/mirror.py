"""Local mirror of remote files: pull before native Read/Edit/Write, push after.

~/.gekong/mirror/<host>/<remote path> holds the file Claude Code sees.
~/.gekong/base/<host>/<remote path> holds the sha256 of the remote content the
mirror was last synced with. Before an edit, the remote is checked against it,
and the hash that check accepted is pinned to the tool call (by tool_use_id);
the write-back afterwards is compare-and-swap against that pinned hash, so a
remote change made behind our back - by anyone, including another agent sharing
the mirror - is never overwritten.
"""
import hashlib
import json
import os
import random
import re
import string
import time
from pathlib import Path

from . import paths, ssh

MAX_PULL_BYTES = int(os.environ.get("GEKONG_MAX_PULL_BYTES", 20 * 1024 * 1024))
PENDING_MAX_AGE = 24 * 3600
STORED = object()  # push(): compare against the stored base


def _base_file(host: str, rpath: str) -> Path:
    return paths.base_root() / host / (rpath.lstrip("/") + ".sha256")


def get_base(host: str, rpath: str):
    try:
        return _base_file(host, rpath).read_text().strip() or None
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def set_base(host: str, rpath: str, sha: str) -> None:
    _atomic_write(_base_file(host, rpath), sha.encode())


def clear_base(host: str, rpath: str) -> None:
    try:
        _base_file(host, rpath).unlink()
    except (FileNotFoundError, NotADirectoryError):
        pass


def file_sha(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    paths.ensure_home()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.gekong-{''.join(random.choices(string.ascii_lowercase, k=6))}"
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _drop_mirror(host: str, rpath: str) -> None:
    mpath = paths.mirror_path(host, rpath)
    if mpath.is_file() or mpath.is_symlink():
        mpath.unlink()
    clear_base(host, rpath)


def _fetch(host: str, rpath: str, have):
    resp, data = ssh.call(host, {"op": "read", "path": rpath, "have": have, "max_bytes": MAX_PULL_BYTES})
    return resp, data


def _size_msg(resp) -> str:
    mb = resp.get("size", 0) / 1e6
    return f"{mb:.1f} MB (over gekong's {MAX_PULL_BYTES / 1e6:.0f} MB mirror limit)"


def pull(host: str, rpath: str):
    """Bring the mirror copy up to date. Returns None, or a reason to deny the Read."""
    mpath = paths.mirror_path(host, rpath)
    resp, data = _fetch(host, rpath, file_sha(mpath))
    if not resp["exists"]:
        _drop_mirror(host, rpath)
        return None  # Read then reports "file does not exist" by itself
    if resp["kind"] == "dir":
        return (f"{rpath} is a directory on {host}. List it with the gekong Glob tool "
                f"(host={host!r}, path={rpath!r}).")
    if resp["kind"] != "file":
        return f"{rpath} on {host} is not a regular file."
    if resp.get("too_large"):
        return (f"{rpath} on {host} is {_size_msg(resp)}. Search it with the gekong Grep tool, or print a "
                f"line range with `gk run {host} \"sed -n '100,200p' {rpath}\"`.")
    if "data_len" in resp:
        _atomic_write(mpath, data)
    set_base(host, rpath, resp["sha"])
    return None


def check_before_write(host: str, rpath: str):
    """Before Edit/Write: refuse if the remote changed since the mirror was synced.

    Returns (reason, expect). reason is None to allow the edit, else why it was
    denied (after refreshing the mirror, so the next Read sees the remote
    version). expect is the remote hash the check accepted (None: no such file),
    which the write-back must still find.
    """
    mpath = paths.mirror_path(host, rpath)
    base = get_base(host, rpath)
    have = base or file_sha(mpath)
    resp, data = _fetch(host, rpath, have)
    if not resp["exists"]:
        if base is None:
            return None, None  # a new file
        _drop_mirror(host, rpath)
        return f"{rpath} was deleted on {host} since you read it. Check with the user before recreating it.", None
    if resp["kind"] != "file":
        return f"{rpath} on {host} is not a regular file.", None
    if resp.get("too_large"):
        return f"{rpath} on {host} is {_size_msg(resp)}; gekong will not edit it through the mirror.", None
    if resp["sha"] == have:
        if base is None:
            set_base(host, rpath, resp["sha"])
        return None, resp["sha"]
    _atomic_write(mpath, data)
    set_base(host, rpath, resp["sha"])
    if base is None and have is None:
        return f"{rpath} already exists on {host}. Read it first, then edit it.", None
    return (f"{rpath} was changed on {host} since you last read it, so this edit was not made. "
            "Read it again, then redo the edit."), None


def _pending_file(tool_use_id: str) -> Path:
    return paths.home() / "pending" / (re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id) + ".json")


def remember(tool_use_id: str, host: str, rpath: str, expect) -> None:
    """Pin the hash an edit was checked against until its write-back."""
    d = paths.ensure_private(paths.home() / "pending")
    now = time.time()
    for f in d.iterdir():  # calls that never reached PostToolUse (denied, failed)
        try:
            if now - f.stat().st_mtime > PENDING_MAX_AGE:
                f.unlink()
        except OSError:
            pass
    _atomic_write(_pending_file(tool_use_id), json.dumps({"host": host, "path": rpath, "expect": expect}).encode())


def recall(tool_use_id: str, host: str, rpath: str):
    """The hash pinned by remember(), or STORED if there is none for this call."""
    f = _pending_file(tool_use_id)
    try:
        rec = json.loads(f.read_text())
        f.unlink()
    except (OSError, ValueError):
        return STORED
    if rec.get("host") != host or rec.get("path") != rpath:
        return STORED
    return rec.get("expect")


def push(host: str, rpath: str, expect=STORED):
    """After a successful Edit/Write: write the mirror back to the remote, provided
    it still holds `expect` (by default the stored base).

    Returns None on success, or a message for the model when the edit did not
    reach the remote.
    """
    mpath = paths.mirror_path(host, rpath)
    if expect is STORED:
        expect = get_base(host, rpath)
    try:
        data = mpath.read_bytes()
        resp, _ = ssh.call(host, {"op": "write", "path": rpath, "expect": expect}, data)
    except ssh.GekongError as e:
        _drop_mirror(host, rpath)
        return (f"The edit to {rpath} was NOT written to {host}: {e} "
                "The local mirror copy was discarded.")
    if resp.get("ok"):
        set_base(host, rpath, resp["sha"])
        return None
    # Someone changed the remote file between our check and the write.
    try:
        pull(host, rpath)
    except ssh.GekongError:
        _drop_mirror(host, rpath)
    return (f"The edit to {rpath} was NOT written to {host}: the remote file changed while the edit was "
            "being made. The mirror now holds the remote version; Read it again and redo the edit.")

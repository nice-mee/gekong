"""Local registry of long-running gk commands, for `gekong ps / tail / kill`.

Each run gets ~/.gekong/runs/<id>/ with meta.json and out.log; gk writes both
itself, so there is no daemon.
"""
import json
import os
import random
import shutil
import string
import subprocess
import time
from pathlib import Path

from . import paths

KEEP_SECONDS = 7 * 24 * 3600


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


class Run:
    def __init__(self, host: str, argv: list):
        root = paths.ensure_private(paths.runs_dir())
        prune(root)
        while True:
            self.id = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
            self.dir = root / self.id
            try:
                self.dir.mkdir()
                break
            except FileExistsError:
                continue
        self.meta = {"id": self.id, "host": host, "argv": argv, "cwd": os.getcwd(),
                     "pid": os.getpid(), "pid_start": proc_start(os.getpid()), "started": time.time()}
        _write_json(self.dir / "meta.json", self.meta)
        self.log = open(self.dir / "out.log", "ab")

    def finish(self, code: int) -> None:
        self.log.close()
        self.meta.update(ended=time.time(), code=code)
        _write_json(self.dir / "meta.json", self.meta)


def prune(root: Path) -> None:
    now = time.time()
    for d in root.iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if now - meta.get("ended", meta.get("started", now)) > KEEP_SECONDS and status(meta) != "running":
            shutil.rmtree(d, ignore_errors=True)


def proc_start(pid: int):
    """When a process started, as ps reports it; None if there is no such process."""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    start = r.stdout.strip()
    return start if r.returncode == 0 and start else None


def same_process(meta: dict) -> bool:
    """Is the recorded gk still the process with that pid (not a reuse of the pid)?"""
    start = proc_start(meta["pid"])
    return start is not None and start == meta.get("pid_start")


def status(meta: dict) -> str:
    if "ended" in meta:
        return f"exited {meta.get('code')}"
    return "running" if same_process(meta) else "lost"


def load_all() -> list:
    root = paths.runs_dir()
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        meta["status"] = status(meta)
        meta["log"] = str(d / "out.log")
        out.append(meta)
    out.sort(key=lambda m: m["started"])
    return out


def find(run_id: str) -> dict:
    for m in load_all():
        if m["id"] == run_id:
            return m
    raise KeyError(run_id)

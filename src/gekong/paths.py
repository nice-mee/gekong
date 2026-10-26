"""Local state layout under ~/.gekong (GEKONG_HOME overrides it, e.g. in tests).

Kept to os/pathlib only: the Claude Code hook imports this on every file tool call.
"""
import os
import stat
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("GEKONG_HOME") or os.path.expanduser("~/.gekong"))


def ctl_dir() -> Path:
    return home() / "ctl"


def mirror_root() -> Path:
    return home() / "mirror"


def base_root() -> Path:
    return home() / "base"


def hosts_dir() -> Path:
    return home() / "hosts"


def runs_dir() -> Path:
    return home() / "runs"


def ensure_home() -> Path:
    """Create GEKONG_HOME owner-only: the mirror holds copies of remote files,
    some of them private, and ctl/ holds the sockets that grant the connection."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(h.stat().st_mode) != 0o700:
        os.chmod(h, 0o700)
    return h


def ensure_private(d: Path) -> Path:
    ensure_home()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def check_host(host: str) -> str:
    if not host or host.startswith("-") or "/" in host or any(c.isspace() for c in host) or host in (".", ".."):
        raise ValueError(f"invalid host {host!r}")
    return host


def mirror_path(host: str, remote_path: str) -> Path:
    return mirror_root() / host / remote_path.lstrip("/")


def mirror_prefix(host: str) -> str:
    """String prepended to an absolute remote path to get its mirror path."""
    return str(mirror_root() / host)


def _roots():
    root = mirror_root()
    out = {os.path.normpath(os.path.abspath(root))}
    out.add(os.path.realpath(root))
    return out


def split_mirror_path(path: str):
    """Map a mirror path to (host, absolute remote path), or None if it is outside the mirror."""
    if not path:
        return None
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    for root in _roots():
        if p.startswith(root + os.sep):
            rest = p[len(root) + 1:]
            host, _, remote = rest.partition(os.sep)
            return host, "/" + remote
    return None


def is_under_mirror(path: str) -> bool:
    p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    return any(p == r or p.startswith(r + os.sep) for r in _roots())

"""`gekong setup claude`: wire gekong into Claude Code (user scope).

- ~/.claude/settings.json: the mirror as an additional working directory, the
  file hooks; the remote-work and read-only gekong commands and the gekong
  search tools allowed; plain `ssh` and the gekong commands that log in, cut
  connections, kill or reconfigure kept on ask.
- MCP server `gekong` registered with `claude mcp add --scope user`.
- The gekong skill symlinked to ~/.claude/skills/gekong.

Every change is idempotent and undone by `--uninstall`; settings.json is backed
up before it is rewritten. Only what gekong itself added is ever removed: the
permission entries it added are recorded in ~/.gekong/setup-claude.json (an
identical entry the user already had stays theirs), and the skill link is only
replaced or removed while it points at gekong's skill.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from . import paths

HOOK_MARK = "gekong-hook"
PRE_MATCHER = "Read|Edit|Write|MultiEdit|NotebookEdit|Grep|Glob"
POST_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
ALLOW = ["Bash(gekong run:*)", "Bash(gekong job:*)", "Bash(gekong wait:*)",
         "Bash(gekong status:*)", "Bash(gekong ps:*)", "Bash(gekong tail:*)",
         "mcp__gekong__Grep", "mcp__gekong__Glob"]
# `gekong up` may need 2FA and the skill says to hand it to the user; the rest change state.
# `gekong ssh` passes any ssh option through (-L/-R/-D forwards, -O exit, -o ...), so it is
# plain ssh as far as permissions go.
ASK = ["Bash(ssh:*)", "Bash(gekong ssh:*)", "Bash(gekong up:*)", "Bash(gekong down:*)",
       "Bash(gekong kill:*)", "Bash(gekong setup:*)"]


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


def _is_ours(group: dict) -> bool:
    return any(HOOK_MARK in (h.get("command") or "") for h in group.get("hooks", []))


def _wanted(mirror: str) -> dict:
    return {"allow": ALLOW, "ask": ASK, "additionalDirectories": [mirror]}


def _strip(settings: dict, owned: dict) -> dict:
    s = json.loads(json.dumps(settings))
    hooks = s.get("hooks", {})
    for event in ("PreToolUse", "PostToolUse"):
        if event in hooks:
            hooks[event] = [g for g in hooks[event] if not _is_ours(g)]
            if not hooks[event]:
                del hooks[event]
    if "hooks" in s and not s["hooks"]:
        del s["hooks"]
    perms = s.get("permissions", {})
    for key, entries in owned.items():
        if key in perms:
            perms[key] = [x for x in perms[key] if x not in entries]
            if not perms[key]:
                del perms[key]
    if "permissions" in s and not s["permissions"]:
        del s["permissions"]
    return s


def merge(settings: dict, hook_cmd: str, mirror: str, owned: dict = None):
    """Return (settings with gekong's entries (re)applied, the permission entries
    gekong now owns). `owned` is what a previous install added. Entries the user
    already had are neither touched nor owned, so uninstall leaves them."""
    s = _strip(settings, owned or {})
    hooks = s.setdefault("hooks", {})
    entry = lambda m: {"matcher": m, "hooks": [{"type": "command", "command": hook_cmd, "timeout": 120}]}
    hooks.setdefault("PreToolUse", []).append(entry(PRE_MATCHER))
    hooks.setdefault("PostToolUse", []).append(entry(POST_MATCHER))
    perms = s.setdefault("permissions", {})
    added = {}
    for key, wanted in _wanted(mirror).items():
        lst = perms.setdefault(key, [])
        for x in wanted:
            if x not in lst:
                lst.append(x)
                added.setdefault(key, []).append(x)
    return s, added


def unmerge(settings: dict, owned: dict) -> dict:
    return _strip(settings, owned)


def _state_file() -> Path:
    return paths.home() / "setup-claude.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save(path: Path, settings: dict) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.gekong-bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"  backed up {path} -> {backup.name}")
    tmp = path.with_name(path.name + ".gekong-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _exe(name: str) -> str:
    found = shutil.which(name) or str(Path(sys.argv[0]).resolve().parent / name)
    if not os.access(found, os.X_OK):
        raise SystemExit(f"gekong: cannot find the {name} executable; install gekong first (uv tool install -e .)")
    return found


def _claude(*args) -> bool:
    if not shutil.which("claude"):
        return False
    return subprocess.run(["claude", *args], capture_output=True, text=True).returncode == 0


def skill_source() -> Path:
    return Path(str(resources.files("gekong").joinpath("skill")))


def _is_our_link(link: Path) -> bool:
    target = os.readlink(link)
    return (link.exists() and link.resolve() == skill_source().resolve()) or \
        os.path.normpath(target).endswith(os.path.join("gekong", "skill"))


def link_skill() -> str:
    link = claude_dir() / "skills" / "gekong"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if not _is_our_link(link):
            return f"! {link} links to {os.readlink(link)}, not gekong's skill; left it alone"
        link.unlink()
    elif link.exists():
        return f"! {link} exists and is not a symlink; left it alone"
    link.symlink_to(skill_source(), target_is_directory=True)
    return f"linked skill {link} -> {skill_source()}"


def unlink_skill():
    link = claude_dir() / "skills" / "gekong"
    if link.is_symlink() and _is_our_link(link):
        link.unlink()
        return f"removed {link}"
    return None


def install(dry_run: bool = False) -> int:
    hook_cmd, mcp_cmd = _exe("gekong-hook"), _exe("gekong-mcp")
    mirror = str(paths.mirror_root())
    settings_path = claude_dir() / "settings.json"
    state = _load_state()
    new, owned = merge(_load(settings_path), hook_cmd, mirror, state.get("owned"))
    if dry_run:
        print(json.dumps(new, indent=2, ensure_ascii=False))
        print(f"\n(dry run) would also: claude mcp add -s user gekong -- {mcp_cmd}; "
              f"link {claude_dir() / 'skills' / 'gekong'} -> {skill_source()}")
        return 0
    paths.ensure_home()
    paths.mirror_root().mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _save(settings_path, new)
    _state_file().write_text(json.dumps({"owned": owned}, indent=2))
    print(f"  updated {settings_path}: hooks, permissions, additional directory {mirror}")

    _claude("mcp", "remove", "--scope", "user", "gekong")
    if _claude("mcp", "add", "--scope", "user", "gekong", "--", mcp_cmd):
        print(f"  registered MCP server gekong -> {mcp_cmd}")
    else:
        print(f"  ! could not register the MCP server; run: claude mcp add --scope user gekong -- {mcp_cmd}")
    print("  " + link_skill())
    print("gekong: Claude Code is set up; restart running sessions to pick it up.")
    return 0


def uninstall() -> int:
    settings_path = claude_dir() / "settings.json"
    if settings_path.exists():
        _save(settings_path, unmerge(_load(settings_path), _load_state().get("owned", {})))
        print(f"  removed gekong entries from {settings_path}")
    _state_file().unlink(missing_ok=True)
    if _claude("mcp", "remove", "--scope", "user", "gekong"):
        print("  removed MCP server gekong")
    msg = unlink_skill()
    if msg:
        print("  " + msg)
    return 0

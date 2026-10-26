"""Claude Code hooks that make native file tools work on the remote via the mirror.

Registered (by `gekong setup claude`) as one command for PreToolUse on
Read|Edit|Write|MultiEdit|NotebookEdit|Grep|Glob and PostToolUse on the
editing tools. Anything outside ~/.gekong/mirror returns immediately, so the
module imports only the standard library until it knows the call is ours.
"""
import json
import os
import sys

from . import paths

FILE_PARAM = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "gekong: " + reason}}


def block(reason: str) -> dict:
    return {"decision": "block", "reason": "gekong: " + reason}


def search_redirect(event: dict):
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("path") or event.get("cwd") or ""
    if not paths.is_under_mirror(target):
        return None
    loc = paths.split_mirror_path(target)
    hint = f" (host={loc[0]!r}, path={loc[1]!r})" if loc and loc[0] else ""
    return deny(f"the mirror only holds files that were already read, so native {event['tool_name']} "
                f"there misses most of the remote. Use the gekong MCP {event['tool_name']} tool{hint} instead.")


def handle(event: dict):
    tool = event.get("tool_name")
    phase = event.get("hook_event_name")
    if tool in ("Grep", "Glob"):
        return search_redirect(event) if phase == "PreToolUse" else None
    param = FILE_PARAM.get(tool)
    if param is None:
        return None
    loc = paths.split_mirror_path((event.get("tool_input") or {}).get(param, ""))
    if loc is None:
        return None
    host, rpath = loc
    if not host or rpath == "/":
        return None

    from . import mirror, ssh  # only now: these pull in subprocess and friends

    try:
        paths.check_host(host)
        tool_use_id = event.get("tool_use_id")
        if phase == "PreToolUse" and tool == "Read":
            reason = mirror.pull(host, rpath)
            return deny(reason) if reason else None
        if phase == "PreToolUse":
            reason, expect = mirror.check_before_write(host, rpath)
            if reason:
                return deny(reason)
            if tool_use_id:
                mirror.remember(tool_use_id, host, rpath, expect)
            return None
        if phase == "PostToolUse" and tool in EDIT_TOOLS:
            expect = mirror.recall(tool_use_id, host, rpath) if tool_use_id else mirror.STORED
            msg = mirror.push(host, rpath, expect)
            return block(msg) if msg else None
    except ssh.GekongError as e:
        if phase == "PreToolUse":
            return deny(str(e))
        return block(str(e))
    except ValueError as e:
        return deny(str(e)) if phase == "PreToolUse" else block(str(e))
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        out = handle(event)
    except Exception as e:
        # Never let a crash on a mirror path turn into a silently unsynced edit,
        # and never let it get in the way of tools working on local files.
        tool_input = event.get("tool_input") or {}
        target = tool_input.get(FILE_PARAM.get(event.get("tool_name"), "path")) or ""
        if not (isinstance(target, str) and target and paths.is_under_mirror(target)):
            return 0
        out = (deny if event.get("hook_event_name") == "PreToolUse" else block)(
            f"internal error ({type(e).__name__}: {e}); the remote file was not touched.")
    if out:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
